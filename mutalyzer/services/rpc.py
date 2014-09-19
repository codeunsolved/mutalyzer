"""
Mutalyzer RPC services.

@todo: More thourough input checking. The @soap decorator does not do any
       kind of strictness checks on the input. For example, in
       transcriptInfo, the build argument must really be present.
       We should use the built-in validator functionality from Spyne for
       this.
"""


from spyne.decorator import srpc
from spyne.service import ServiceBase
from spyne.model.primitive import String, Integer, Boolean, DateTime
from spyne.model.complex import Array
from spyne.model.fault import Fault
import os
import socket
from cStringIO import StringIO
import tempfile
from operator import itemgetter, attrgetter
from sqlalchemy.orm.exc import NoResultFound

import mutalyzer
from mutalyzer.config import settings
from mutalyzer.db import session
from mutalyzer.db.models import (Assembly, Chromosome, BatchJob,
                                 BatchQueueItem, TranscriptMapping)
from mutalyzer.output import Output
from mutalyzer.grammar import Grammar
from mutalyzer.sync import CacheSync
from mutalyzer import stats
from mutalyzer import variantchecker
from mutalyzer.mapping import Converter
from mutalyzer import File
from mutalyzer import Retriever
from mutalyzer import GenRecord
from mutalyzer import Scheduler
from mutalyzer.models import *
from mutalyzer import describe


class MutalyzerService(ServiceBase):
    """
    Mutalyzer web services.

    These methods are made public via a SOAP interface.
    """
    def __init__(self, environ=None):
        super(MutalyzerService, self).__init__(environ)
    #__init__

    @srpc(Mandatory.ByteArray, String, String,  _returns=String)
    def submitBatchJob(data, process='NameChecker', argument=''):
        """
        Submit a batch job.

        Input and output file formats for batch jobs are explained on the
        website <https://mutalyzer.nl/batch>.

        On error an exception is raised:
          - detail: Human readable description of the error.
          - faultstring: A code to indicate the type of error.
              - EPARSE: The batch input could not be parsed.
              - EMAXSIZE: Input file exceeds maximum size.

        @arg data: Input file.
        @arg process: Optional type of the batch job, choose from: NameChecker
            (default), SyntaxChecker, PositionConverter, SnpConverter.
        @arg argument: Additional argument. Currently only used if batch_type
            is PositionConverter, denoting the human genome build.

        @return: Batch job identifier.
        """
        output = Output(__file__)

        stats.increment_counter('batch-job/webservice')

        scheduler = Scheduler.Scheduler()
        file_instance = File.File(output)

        batch_types = {'NameChecker': 'name-checker',
                       'SyntaxChecker': 'syntax-checker',
                       'PositionConverter': 'position-converter',
                       'SnpConverter': 'snp-converter'}

        if process not in batch_types:
            raise Fault('EARG',
                        'The process argument must be one of %s.'
                        % ', '.join(batch_types))

        # Note that the max file size check below might be bogus, since Spyne
        # first checks the total request size, which by default has a maximum
        # of 2 megabytes.
        # In that case, a senv:Client.RequestTooLong faultstring is returned.

        # Todo: Set maximum request size by specifying the max_content_length
        #     argument for spyne.server.wsgi.WsgiApplication in all webservice
        #     instantiations.
        if sum(len(s) for s in data) > settings.MAX_FILE_SIZE:
            raise Fault('EMAXSIZE',
                        'Only files up to %d megabytes are accepted.'
                        % (settings.MAX_FILE_SIZE // 1048576))

        batch_file = StringIO(''.join(data))

        job, columns = file_instance.parseBatchFile(batch_file)
        batch_file.close()

        if job is None:
            raise Fault('EPARSE', 'Could not parse input file, please check your file format.')

        result_id = scheduler.addJob('job@webservice', job, columns,
                                     batch_types[process], argument)
        return result_id

    @srpc(Mandatory.String, _returns=Integer)
    def monitorBatchJob(job_id):
        """
        Get the number of entries left for a batch job.

        Input and output file formats for batch jobs are explained on the
        website <https://mutalyzer.nl/batch>.

        @arg job_id: Batch job identifier.

        @return: Number of entries left.
        """
        return BatchQueueItem.query.join(BatchJob).filter_by(result_id=job_id).count()

    @srpc(Mandatory.String, _returns=ByteArray)
    def getBatchJob(job_id):
        """
        Get the result of a batch job.

        Input and output file formats for batch jobs are explained on the
        website <https://mutalyzer.nl/batch>.

        On error an exception is raised:
          - detail: Human readable description of the error.
          - faultstring: A code to indicate the type of error.
              - EBATCHNOTREADY: The batch job result is not yet ready.

        @arg job_id: Batch job identifier.

        @return: Batch job result file.
        """
        left = BatchQueueItem.query.join(BatchJob).filter_by(result_id=job_id).count()

        if left > 0:
            raise Fault('EBATCHNOTREADY', 'Batch job result is not yet ready.')

        filename = 'batch-job-%s.txt' % job_id
        handle = open(os.path.join(settings.CACHE_DIR, filename))
        return handle

    @srpc(Mandatory.String, Mandatory.String, Mandatory.Integer, Boolean,
        _returns=Array(Mandatory.String))
    def getTranscripts(build, chrom, pos, versions=False) :
        """
        Get all the transcripts that overlap with a chromosomal position.

        On error an exception is raised:
          - detail       ; Human readable description of the error.
          - faultstring: ; A code to indicate the type of error.
              - EARG   ; The argument was not valid.
              - ERANGE ; An invalid range was given.

        @arg build: The genome build (hg19, hg18, mm10).
        @type build: string
        @arg chrom: A chromosome encoded as "chr1", ..., "chrY".
        @type chrom: string
        @arg pos: A position on the chromosome.
        @type pos: int
        @kwarg versions: If set to True, also include transcript versions.
        @type versions: bool

        @return: A list of transcripts.
        @rtype: list
        """
        L = Output(__file__)

        L.addMessage(__file__, -1, "INFO",
            "Received request getTranscripts(%s %s %s %s)" % (build, chrom,
            pos, versions))

        try:
            assembly = Assembly.by_name_or_alias(build)
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % build)
            raise Fault("EARG",
                        "The build argument (%s) was not a valid " \
                            "build name." % build)

        try:
            chromosome = assembly.chromosomes.filter_by(name=chrom).one()
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % chrom)
            raise Fault("EARG", "The chrom argument (%s) was not a valid " \
                            "chromosome name." % chrom)

        mappings = chromosome.transcript_mappings.filter(
            TranscriptMapping.start <= pos,
            TranscriptMapping.stop >= pos)

        L.addMessage(__file__, -1, "INFO",
                     "Finished processing getTranscripts(%s %s %s %s)"
                     % (build, chrom, pos, versions))

        #filter out the accNo
        if versions:
            return ['%s.%s' % (m.accession, m.version) for m in mappings]
        else:
            return [m.accession for m in mappings]
    #getTranscripts

    @srpc(Mandatory.String, Mandatory.String, _returns=Array(Mandatory.String))
    def getTranscriptsByGeneName(build, name):
        """
        Todo: documentation.
        """
        L = Output(__file__)

        L.addMessage(__file__, -1, "INFO",
            "Received request getTranscriptsByGene(%s %s)" % (build, name))

        try:
            assembly = Assembly.by_name_or_alias(build)
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % build)
            raise Fault("EARG",
                        "The build argument (%s) was not a valid " \
                            "build name." % build)

        mappings = TranscriptMapping.query \
            .filter(TranscriptMapping.chromosome.has(assembly=assembly),
                    TranscriptMapping.gene == name)

        L.addMessage(__file__, -1, "INFO",
            "Finished processing getTranscriptsByGene(%s %s)" % (build, name))

        return ['%s.%s' % (m.accession, m.version) for m in mappings]
    #getTranscriptsByGene

    @srpc(Mandatory.String, Mandatory.String, Mandatory.Integer,
        Mandatory.Integer, Mandatory.Integer, _returns=Array(Mandatory.String))
    def getTranscriptsRange(build, chrom, pos1, pos2, method) :
        """
        Get all the transcripts that overlap with a range on a chromosome.

        @arg build: The genome build (hg19, hg18, mm10).
        @type build: string
        @arg chrom: A chromosome encoded as "chr1", ..., "chrY".
        @type chrom: string
        @arg pos1: The first postion of the range.
        @type pos1: integer
        @arg pos2: The last postion of the range.
        @type pos2: integer
        @arg method: The method of determining overlap:
            - 0 ; Return only the transcripts that completely fall in the range
                  [pos1, pos2].
            - 1 ; Return all hit transcripts.

        @return: A list of transcripts.
        @rtype: list
        """
        L = Output(__file__)

        L.addMessage(__file__, -1, "INFO",
            "Received request getTranscriptsRange(%s %s %s %s %s)" % (build,
            chrom, pos1, pos2, method))

        try:
            assembly = Assembly.by_name_or_alias(build)
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % build)
            raise Fault("EARG",
                        "The build argument (%s) was not a valid " \
                            "build name." % build)

        try:
            chromosome = assembly.chromosomes.filter_by(name=chrom).one()
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % chrom)
            raise Fault("EARG", "The chrom argument (%s) was not a valid " \
                            "chromosome name." % chrom)

        if method:
            range_filter = (TranscriptMapping.start <= pos2,
                            TranscriptMapping.stop >= pos1)
        else:
            range_filter = (TranscriptMapping.start >= pos1,
                            TranscriptMapping.stop <= pos2)

        mappings = chromosome.transcript_mappings.filter(*range_filter)

        L.addMessage(__file__, -1, "INFO",
            "Finished processing getTranscriptsRange(%s %s %s %s %s)" % (
            build, chrom, pos1, pos2, method))

        return [m.accession for m in mappings]
    #getTranscriptsRange

    @srpc(Mandatory.String, Mandatory.String, Mandatory.Integer,
        Mandatory.Integer, Mandatory.Integer,
        _returns=Array(TranscriptMappingInfo))
    def getTranscriptsMapping(build, chrom, pos1, pos2, method):
        """
        Get all the transcripts and their info that overlap with a range on a
        chromosome.

        @arg build: The genome build (hg19, hg18, mm10).
        @type build: string
        @arg chrom: A chromosome encoded as "chr1", ..., "chrY".
        @type chrom: string
        @arg pos1: The first postion of the range.
        @type pos1: integer
        @arg pos2: The last postion of the range.
        @type pos2: integer
        @arg method: The method of determining overlap:
            - 0 ; Return only the transcripts that completely fall in the range
                  [pos1, pos2].
            - 1 ; Return all hit transcripts.

        @return: Array of TranscriptMappingInfo objects with fields:
                 - name
                 - version
                 - gene
                 - orientation
                 - start
                 - stop
                 - cds_start
                 - cds_stop
        """
        output = Output(__file__)
        output.addMessage(__file__, -1, 'INFO', 'Received request ' \
            'getTranscriptsMapping(%s %s %s %s %s)' % (build, chrom, pos1, pos2,
            method))

        try:
            assembly = Assembly.by_name_or_alias(build)
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % build)
            raise Fault("EARG",
                        "The build argument (%s) was not a valid " \
                            "build name." % build)

        try:
            chromosome = assembly.chromosomes.filter_by(name=chrom).one()
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % chrom)
            raise Fault("EARG", "The chrom argument (%s) was not a valid " \
                            "chromosome name." % chrom)

        if method:
            range_filter = (TranscriptMapping.start <= pos2,
                            TranscriptMapping.stop >= pos1)
        else:
            range_filter = (TranscriptMapping.start >= pos1,
                            TranscriptMapping.stop <= pos2)

        mappings = chromosome.transcript_mappings.filter(*range_filter)

        transcripts = []

        for mapping in mappings:
            t = TranscriptMappingInfo()
            t.name = mapping.accession
            t.version = mapping.version
            t.gene = mapping.gene
            t.orientation = '-' if mapping.orientation == 'reverse' else '+'
            if mapping.orientation == 'reverse':
                t.start, t.stop = mapping.stop, mapping.start
            else:
                t.start, t.stop = mapping.start, mapping.stop
            if mapping.orientation == 'reverse':
                t.cds_start, t.cds_stop = mapping.cds_stop, mapping.cds_start
            else:
                t.cds_start, t.cds_stop = mapping.cds_start, mapping.cds_stop
            transcripts.append(t)

        output.addMessage(__file__, -1, 'INFO', 'Finished processing ' \
            'getTranscriptsMapping(%s %s %s %s %s)' % (build, chrom, pos1, pos2,
            method))

        return transcripts
    #getTranscriptsMapping

    @srpc(Mandatory.String, Mandatory.String, _returns=Mandatory.String)
    def getGeneName(build, accno) :
        """
        Find the gene name associated with a transcript.

        @arg build: The genome build (hg19, hg18, mm10).
        @type build: string
        @arg accno: The identifier of a transcript.
        @type accno: string

        @return: The name of the associated gene.
        @rtype: string
        """
        L = Output(__file__)

        L.addMessage(__file__, -1, "INFO",
            "Received request getGeneName(%s %s)" % (build, accno))

        try:
            assembly = Assembly.by_name_or_alias(build)
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % build)
            raise Fault("EARG",
                        "The build argument (%s) was not a valid " \
                            "build name." % build)

        mapping = TranscriptMapping.query \
            .filter(TranscriptMapping.chromosome.has(assembly=assembly),
                    TranscriptMapping.accession == accno.split('.')[0]) \
            .first()

        L.addMessage(__file__, -1, "INFO",
            "Finished processing getGeneName(%s %s)" % (build, accno))

        return mapping.gene
    #getGeneName

    @srpc(Mandatory.String, Mandatory.String, Mandatory.String,
        Mandatory.String, _returns=Mapping)
    def mappingInfo(LOVD_ver, build, accNo, variant) :
        """
        Search for an NM number in the MySQL database, if the version
        number matches, get the start and end positions in a variant and
        translate these positions to I{g.} notation if the variant is in I{c.}
        notation and vice versa.

          - If no end position is present, the start position is assumed to be
            the end position.
          - If the version number is not found in the database, an error
            message is generated and a suggestion for an other version is
            given.
          - If the reference sequence is not found at all, an error is
            returned.
          - If no variant is present, an error is returned.
          - If the variant is not accepted by the nomenclature parser, a parse
            error will be printed.

        @arg LOVD_ver: The LOVD version.
        @type LOVD_ver: string
        @arg build: The genome build (hg19, hg18, mm10).
        @type build: string
        @arg accNo: The NM accession number and version.
        @type accNo: string
        @arg variant: The variant.
        @type variant: string

        @return: Complex object:
          - start_main   ; The main coordinate of the start position
                           in I{c.} (non-star) notation.
          - start_offset ; The offset coordinate of the start position
                           in I{c.} notation (intronic position).
          - end_main     ; The main coordinate of the end position in
                           I{c.} (non-star) notation.
          - end_offset   ; The offset coordinate of the end position in
                           I{c.} notation (intronic position).
          - start_g      ; The I{g.} notation of the start position.
          - end_g        ; The I{g.} notation of the end position.
          - type         ; The mutation type.
        @rtype: object
        """
        L = Output(__file__)

        L.addMessage(__file__, -1, "INFO",
            "Reveived request mappingInfo(%s %s %s %s)" % (LOVD_ver, build,
            accNo, variant))

        try:
            assembly = Assembly.by_name_or_alias(build)
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % build)
            raise Fault("EARG",
                        "The build argument (%s) was not a valid " \
                            "build name." % build)

        conv = Converter(assembly, L)
        result = conv.mainMapping(accNo, variant)

        L.addMessage(__file__, -1, "INFO",
            "Finished processing mappingInfo(%s %s %s %s)" % (LOVD_ver, build,
            accNo, variant))

        del L
        return result
    #mappingInfo

    @srpc(Mandatory.String, Mandatory.String, Mandatory.String,
        _returns=Transcript)
    def transcriptInfo(LOVD_ver, build, accNo) :
        """
        Search for an NM number in the MySQL database, if the version
        number matches, the transcription start and end and CDS end
        in I{c.} notation is returned.

        @arg LOVD_ver: The LOVD version.
        @type LOVD_ver: string
        @arg build: The genome build (hg19, hg18, mm10).
        @type build: string
        @arg accNo: The NM accession number and version.
        @type accNo: string

        @return: Complex object:
          - trans_start  ; Transcription start in I{c.} notation.
          - trans_stop   ; Transcription stop in I{c.} notation.
          - CDS_stop     ; CDS stop in I{c.} notation.
        @rtype: object
        """
        O = Output(__file__)

        O.addMessage(__file__, -1, "INFO",
            "Received request transcriptInfo(%s %s %s)" % (LOVD_ver, build,
            accNo))

        try:
            assembly = Assembly.by_name_or_alias(build)
        except NoResultFound:
            O.addMessage(__file__, 4, "EARG", "EARG %s" % build)
            raise Fault("EARG",
                        "The build argument (%s) was not a valid " \
                            "build name." % build)

        converter = Converter(assembly, O)
        T = converter.mainTranscript(accNo)

        O.addMessage(__file__, -1, "INFO",
            "Finished processing transcriptInfo(%s %s %s)" % (LOVD_ver, build,
            accNo))
        return T
    #transcriptInfo

    @srpc(Mandatory.String, Mandatory.String, _returns=Mandatory.String)
    def chromAccession(build, name) :
        """
        Get the accession number of a chromosome, given a name.

        @arg build: The genome build (hg19, hg18, mm10).
        @type build: string
        @arg name: The name of a chromosome (e.g. chr1).
        @type name: string

        @return: The accession number of a chromosome.
        @rtype: string
        """
        L = Output(__file__)
        L.addMessage(__file__, -1, "INFO",
            "Received request chromAccession(%s %s)" % (build, name))

        try:
            assembly = Assembly.by_name_or_alias(build)
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % build)
            raise Fault("EARG",
                        "The build argument (%s) was not a valid " \
                            "build name." % build)

        try:
            chromosome = assembly.chromosomes.filter_by(name=name).one()
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % name)
            raise Fault("EARG", "The name argument (%s) was not a valid " \
                            "chromosome name." % name)

        L.addMessage(__file__, -1, "INFO",
            "Finished processing chromAccession(%s %s)" % (build, name))

        return chromosome.accession
    #chromAccession

    @srpc(Mandatory.String, Mandatory.String, _returns=Mandatory.String)
    def chromosomeName(build, accNo) :
        """
        Get the name of a chromosome, given a chromosome accession number.

        @arg build: The genome build (hg19, hg18, mm10).
        @type build: string
        @arg accNo: The accession number of a chromosome (NC_...).
        @type accNo: string

        @return: The name of a chromosome.
        @rtype: string
        """
        L = Output(__file__)
        L.addMessage(__file__, -1, "INFO",
            "Received request chromName(%s %s)" % (build, accNo))

        try:
            assembly = Assembly.by_name_or_alias(build)
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % build)
            raise Fault("EARG",
                        "The build argument (%s) was not a valid " \
                            "build name." % build)

        try:
            chromosome = assembly.chromosomes.filter_by(accession=accNo).one()
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % accNo)
            raise Fault("EARG", "The accNo argument (%s) was not a valid " \
                            "chromosome accession." % accNo)

        L.addMessage(__file__, -1, "INFO",
            "Finished processing chromName(%s %s)" % (build, accNo))

        return chromosome.name
    #chromosomeName

    @srpc(Mandatory.String, Mandatory.String, _returns=Mandatory.String)
    def getchromName(build, acc) :
        """
        Get the chromosome name, given a transcript identifier (NM number).

        @arg build: The genome build (hg19, hg18, mm10).
        @type build: string
        @arg acc: The NM accession number (version NOT included).
        @type acc: string

        @return: The name of a chromosome.
        @rtype: string
        """
        L = Output(__file__)

        L.addMessage(__file__, -1, "INFO",
            "Received request getchromName(%s %s)" % (build, acc))

        try:
            assembly = Assembly.by_name_or_alias(build)
        except NoResultFound:
            L.addMessage(__file__, 4, "EARG", "EARG %s" % build)
            raise Fault("EARG",
                        "The build argument (%s) was not a valid " \
                            "build name." % build)

        mapping = TranscriptMapping.query \
            .filter(TranscriptMapping.chromosome.has(assembly=assembly),
                    TranscriptMapping.accession == acc) \
            .first()

        L.addMessage(__file__, -1, "INFO",
            "Finished processing getchromName(%s %s)" % (build, acc))

        return mapping.chromosome.name
    #chromosomeName

    @srpc(Mandatory.String, Mandatory.String, String,
          _returns=Array(Mandatory.String))
    def numberConversion(build, variant, gene=None):
        """
        Converts I{c.} to I{g.} notation or vice versa

        @arg build: The genome build (hg19, hg18, mm10).
        @type build: string
        @arg variant: The variant in either I{c.} or I{g.} notation, full HGVS
            notation, including NM_ or NC_ accession number.
        @type variant: string
        @kwarg gene: Optional gene name. If given, return variant descriptions
            on all transcripts for this gene.
        @type gene: string

        @return: The variant(s) in either I{g.} or I{c.} notation.
        @rtype: list
        """
        O = Output(__file__)
        O.addMessage(__file__, -1, "INFO",
            "Received request cTogConversion(%s %s)" % (build, variant))

        stats.increment_counter('position-converter/webservice')

        try:
            assembly = Assembly.by_name_or_alias(build)
        except NoResultFound:
            O.addMessage(__file__, 4, "EARG", "EARG %s" % build)
            raise Fault("EARG",
                        "The build argument (%s) was not a valid " \
                            "build name." % build)

        converter = Converter(assembly, O)
        variant = converter.correctChrVariant(variant)

        if "c." in variant or "n." in variant:
            result = [converter.c2chrom(variant)]
        elif "g." in variant or "m." in variant:
            result = converter.chrom2c(variant, "list", gene=gene)
        else:
            result = [""]

        O.addMessage(__file__, -1, "INFO",
            "Finished processing cTogConversion(%s %s)" % (build, variant))
        return result
    #numberConversion

    @srpc(Mandatory.String, _returns=CheckSyntaxOutput)
    def checkSyntax(variant):
        """
        Checks the syntax of a variant.

        @arg variant: The variant to check.
        @type variant: string

        @return: Object with fields:
                 - valid: A boolean indicating parse result (true for
                          succes, false in case of a parse error).
                 - messages: List of (error) messages as strings.
        @rtype: object
        """
        output = Output(__file__)
        output.addMessage(__file__, -1, "INFO",
            "Received request checkSyntax(%s)" % (variant))

        stats.increment_counter('syntax-checker/webservice')

        if not variant :
            output.addMessage(__file__, 4, "EARG", "EARG no variant")
            raise Fault("EARG", "The variant argument is not provided.")

        result = CheckSyntaxOutput()

        grammar = Grammar(output)
        parsetree = grammar.parse(variant)
        result.valid = bool(parsetree)

        output.addMessage(__file__, -1, "INFO",
            "Finished processing checkSyntax(%s)" % (variant))

        result.messages = []
        for message in output.getMessages():
            soap_message = SoapMessage()
            soap_message.errorcode = message.code
            soap_message.message = message.description
            result.messages.append(soap_message)

        return result
    #checkSyntax

    @srpc(Mandatory.String, _returns=MutalyzerOutput)
    def runMutalyzer(variant) :
        """
        Run the Mutalyzer name checker.

        @arg variant: The variant description to check.
        @type variant: string

        @return: Object with fields:
            - referenceId: Identifier of the reference sequence used.
            - sourceId: Identifier of the reference sequence source, e.g. the
                chromosomal accession number and version in case referenceId
                is a  UD reference created as a chromosomal slice.
            - sourceAccession: Accession number of the reference sequence
                source (only for genbank references).
            - sourceVersion: Version number of the reference sequence source
                (only for genbank references).
            - sourceGi: GI number of the reference sequence source (only for
                genbank references).
            - molecule: Molecular type of the reference sequence.
            - original: Original sequence.
            - mutated: Mutated sequence.
            - origMRNA: Original transcript sequence.
            - mutatedMRNA: Mutated transcript sequence.
            - origCDS: Original CDS.
            - newCDS: Mutated CDS.
            - origProtein: Original protein sequence.
            - newProtein: Mutated protein sequence.
            - altProtein: Alternative mutated protein sequence.
            - errors: Number of errors.
            - warnings: Number of warnings.
            - summary: Summary of messages.
            - chromDescription: Chromosomal description.
            - genomicDescription: Genomic description.
            - transcriptDescriptions: List of transcript descriptions.
            - proteinDescriptions: List of protein descriptions.
            - rawVariants: List of raw variants where each raw variant is
                represented by an object with fields:
                - description: Description of the raw variant.
                - visualisation: ASCII visualisation of the raw variant.
            - exons: If a transcript is selected, array of ExonInfo objects
                for each exon in the selected transcript with fields:
                - cStart
                - gStart
                - cStop
                - gStop
            - messages: List of (error) messages.
        """
        O = Output(__file__)
        O.addMessage(__file__, -1, "INFO",
            "Received request runMutalyzer(%s)" % (variant))

        stats.increment_counter('name-checker/webservice')

        variantchecker.check_variant(variant, O)

        result = MutalyzerOutput()

        result.referenceId = O.getIndexedOutput('reference_id', 0)
        result.sourceId = O.getIndexedOutput('source_id', 0)
        result.sourceAccession = O.getIndexedOutput('source_accession', 0)
        result.sourceVersion = O.getIndexedOutput('source_version', 0)
        result.sourceGi = O.getIndexedOutput('source_gi', 0)
        result.molecule = O.getIndexedOutput('molecule', 0)

        # We force the results to strings here, because some results
        # may be of type Bio.Seq.Seq which spyne doesn't like.
        #
        # todo: We might have to also do this elsewhere.

        result.original = str(O.getIndexedOutput("original", 0))
        result.mutated = str(O.getIndexedOutput("mutated", 0))

        result.origMRNA = str(O.getIndexedOutput("origMRNA", 0))
        result.mutatedMRNA = str(O.getIndexedOutput("mutatedMRNA", 0))

        result.origCDS = str(O.getIndexedOutput("origCDS", 0))
        result.newCDS = str(O.getIndexedOutput("newCDS", 0))

        result.origProtein = str(O.getIndexedOutput("oldprotein", 0))
        result.newProtein = str(O.getIndexedOutput("newprotein", 0))
        result.altProtein = str(O.getIndexedOutput("altProtein", 0))

        result.chromDescription = \
            O.getIndexedOutput("genomicChromDescription", 0)
        result.genomicDescription = \
            O.getIndexedOutput("genomicDescription", 0)
        result.transcriptDescriptions = O.getOutput("descriptions")
        result.proteinDescriptions = O.getOutput("protDescriptions")

        if O.getIndexedOutput('hasTranscriptInfo', 0, False):
            result.exons = []
            for e in O.getOutput('exonInfo'):
                exon = ExonInfo()
                exon.gStart, exon.gStop, exon.cStart, exon.cStop = e
                result.exons.append(exon)

        raw_variants = []
        for v in O.getOutput("visualisation"):
            r = RawVariant()
            r.description = v[0]
            r.visualisation = '%s\n%s' % (v[1], v[2])
            raw_variants.append(r)

        result.rawVariants = raw_variants

        result.errors, result.warnings, result.summary = O.Summary()

        O.addMessage(__file__, -1, "INFO",
            "Finished processing runMutalyzer(%s)" % (variant))

        result.messages = []
        for message in O.getMessages():
            soap_message = SoapMessage()
            soap_message.errorcode = message.code
            soap_message.message = message.description
            result.messages.append(soap_message)

        return result
    #runMutalyzer

    @srpc(Mandatory.String, Mandatory.String, _returns=TranscriptNameInfo)
    def getGeneAndTranscript(genomicReference, transcriptReference) :
        """
        Todo: documentation.
        """
        O = Output(__file__)

        O.addMessage(__file__, -1, "INFO",
            "Received request getGeneAndTranscript(%s, %s)" % (
            genomicReference, transcriptReference))
        retriever = Retriever.GenBankRetriever(O)
        record = retriever.loadrecord(genomicReference)

        GenRecordInstance = GenRecord.GenRecord(O)
        GenRecordInstance.record = record
        GenRecordInstance.checkRecord()

        ret = TranscriptNameInfo()
        for i in GenRecordInstance.record.geneList :
            for j in i.transcriptList :
                if j.transcriptID == transcriptReference :
                    ret.transcriptName = "%s_v%s" % (i.name, j.name)
                    ret.productName = j.transcriptProduct
                #if

        O.addMessage(__file__, -1, "INFO",
            "Finished processing getGeneAndTranscript(%s, %s)" % (
            genomicReference, transcriptReference))

        return ret
    #getGeneAndTranscript

    @srpc(Mandatory.String, String, _returns=Array(TranscriptInfo))
    def getTranscriptsAndInfo(genomicReference, geneName=None):
        """
        Given a genomic reference, return all its transcripts with their
        transcription/cds start/end sites and exons.

        @arg genomicReference: Name of a reference sequence.
        @type genomicReference: string

        @arg geneName: Name of gene to restrict returned transcripts to.
                       Default is to return all transcripts.
        @type geneName: string

        @return: Array of TranscriptInfo objects with fields:
                 - name
                 - id
                 - product
                 - cTransStart
                 - gTransStart
                 - chromTransStart
                 - cTransEnd
                 - gTransEnd
                 - chromTransEnd
                 - sortableTransEnd
                 - cCDSStart
                 - gCDSStart
                 - chromCDSStart
                 - cCDSStop
                 - gCDSStop
                 - chromCDSStop
                 - locusTag
                 - linkMethod
                 - exons: Array of ExonInfo objects with fields:
                          - cStart
                          - gStart
                          - chromStart
                          - cStop
                          - gStop
                          - chromStop
                 - proteinTranscript: ProteinTranscript object with fields:
                                      - name
                                      - id
                                      - product
        """
        O = Output(__file__)

        O.addMessage(__file__, -1, "INFO",
            "Received request getTranscriptsAndInfo(%s, %s)" % (
            genomicReference, geneName))
        retriever = Retriever.GenBankRetriever(O)
        record = retriever.loadrecord(genomicReference)

        # Todo: If loadRecord failed (e.g. DTD missing), we should abort here.
        GenRecordInstance = GenRecord.GenRecord(O)
        GenRecordInstance.record = record
        GenRecordInstance.checkRecord()

        transcripts = []

        # The following loop is basically the same as building the legend in
        # the name checker web interface (website.Check).

        for gene in GenRecordInstance.record.geneList:
            # Only return transcripts for requested gene (if there was one)
            if geneName and gene.name != geneName:
                continue
            for transcript in sorted(gene.transcriptList,
                                     key=attrgetter('name')):

                # Exclude nameless transcripts
                if not transcript.name: continue

                t = TranscriptInfo()

                # Some raw info we don't use directly:
                # - transcript.CDS.location        CDS start and stop (g)
                # - transcript.CDS.positionList:   CDS splice sites (g) ?
                # - transcript.mRNA.location:      translation start and stop
                #                                  (g)
                # - transcript.mRNA.positionList:  splice sites (g)

                t.exons = []
                for i in range(0, transcript.CM.numberOfExons() * 2, 2):
                    exon = ExonInfo()
                    exon.gStart = transcript.CM.getSpliceSite(i)
                    exon.cStart = transcript.CM.g2c(exon.gStart)
                    exon.chromStart = GenRecordInstance.record.toChromPos(
                        exon.gStart)
                    exon.gStop = transcript.CM.getSpliceSite(i + 1)
                    exon.cStop = transcript.CM.g2c(exon.gStop)
                    exon.chromStop = GenRecordInstance.record.toChromPos(
                        exon.gStop)
                    t.exons.append(exon)

                # Beware that CM.info() gives a made-up value for trans_end,
                # which is sortable (no * notation). We therefore cannot use
                # it in our output and use the end position of the last exon
                # instead. The made-up value is still useful for sorting, so
                # we return it as sortableTransEnd.
                trans_start, sortable_trans_end, cds_stop = \
                    transcript.CM.info()
                cds_start = 1

                t.cTransEnd = str(t.exons[-1].cStop)
                t.gTransEnd = t.exons[-1].gStop
                t.chromTransEnd = GenRecordInstance.record.toChromPos(
                    t.gTransEnd)
                t.sortableTransEnd = sortable_trans_end

                # Todo: If we have no CDS info, CM.info() gives trans_end as
                # value for cds_stop. This is an artifact to accomodate LOVD
                # stupidity an should probably be removed sometime.
                #if not transcript.CDS: cds_stop = None

                t.name = '%s_v%s' % (gene.name, transcript.name)
                t.id = transcript.transcriptID
                t.product = transcript.transcriptProduct
                t.cTransStart = str(trans_start)
                t.gTransStart = transcript.CM.x2g(trans_start, 0)
                t.chromTransStart = GenRecordInstance.record.toChromPos(
                    t.gTransStart)
                t.cCDSStart = str(cds_start)
                t.gCDSStart = transcript.CM.x2g(cds_start, 0)
                t.chromCDSStart = GenRecordInstance.record.toChromPos(
                    t.gCDSStart)
                t.cCDSStop = str(cds_stop)
                t.gCDSStop = transcript.CM.x2g(cds_stop, 0)
                t.chromCDSStop = GenRecordInstance.record.toChromPos(t.gCDSStop)
                t.locusTag = transcript.locusTag
                t.linkMethod = transcript.linkMethod

                t.proteinTranscript = None

                if transcript.translate:
                    p = ProteinTranscript()
                    p.name = '%s_i%s' % (gene.name, transcript.name)
                    p.id = transcript.proteinID
                    p.product = transcript.proteinProduct
                    t.proteinTranscript = p

                transcripts.append(t)

        O.addMessage(__file__, -1, "INFO",
            "Finished processing getTranscriptsAndInfo(%s)" % genomicReference)

        return transcripts
    #getTranscriptsAndInfo

    @srpc(Mandatory.ByteArray, _returns=Mandatory.String)
    def upLoadGenBankLocalFile(data):
        """
        Upload a genbank file.

        @arg data: Genbank file.
        @return: UD accession number for the uploaded genbank file.
        """
        output = Output(__file__)
        retriever = Retriever.GenBankRetriever(output)

        output.addMessage(__file__, -1, 'INFO',
                          'Received request uploadGenBankLocalFile()')

        # Note that the max file size check below might be bogus, since Spyne
        # first checks the total request size, which by default has a maximum
        # of 2 megabytes.
        # In that case, a senv:Client.RequestTooLong faultstring is returned.

        # Todo: Set maximum request size by specifying the max_content_length
        #     argument for spyne.server.wsgi.WsgiApplication in all webservice
        #     instantiations.
        if sum(len(s) for s in data) > settings.MAX_FILE_SIZE:
            raise Fault('EMAXSIZE',
                        'Only files up to %d megabytes are accepted.'
                        % (settings.MAX_FILE_SIZE // 1048576))

        ud = retriever.uploadrecord(''.join(data))

        output.addMessage(__file__, -1, 'INFO',
                          'Finished processing uploadGenBankLocalFile()')

        # Todo: use SOAP Fault object here (see Trac issue #41).
        if not ud:
            error = 'The request could not be completed\n' \
                    + '\n'.join(map(lambda m: str(m), output.getMessages()))
            raise Exception(error)

        return ud
    #upLoadGenBankLocalFile

    @srpc(Mandatory.String, _returns=Mandatory.String)
    def uploadGenBankRemoteFile(url) :
        """
        Not implemented yet.
        """
        raise Fault('ENOTIMPLEMENTED', 'Not implemented yet')
    #upLoadGenBankRemoteFile

    @srpc(Mandatory.String, Mandatory.String, Mandatory.Integer,
        Mandatory.Integer, _returns=Mandatory.String)
    def sliceChromosomeByGene(geneSymbol, organism, upStream,
        downStream) :
        """
        Todo: documentation, error handling, argument checking, tests.
        """
        O = Output(__file__)
        retriever = Retriever.GenBankRetriever(O)

        O.addMessage(__file__, -1, "INFO",
            "Received request sliceChromosomeByGene(%s, %s, %s, %s)" % (
            geneSymbol, organism, upStream, downStream))

        UD = retriever.retrievegene(geneSymbol, organism, upStream, downStream)

        O.addMessage(__file__, -1, "INFO",
            "Finished processing sliceChromosomeByGene(%s, %s, %s, %s)" % (
            geneSymbol, organism, upStream, downStream))

        # Todo: use SOAP Fault object here (see Trac issue #41).
        if not UD:
            error = 'The request could not be completed\n' \
                    + '\n'.join(map(lambda m: str(m), O.getMessages()))
            raise Exception(error)

        return UD
    #sliceChromosomeByGene

    @srpc(Mandatory.String, Mandatory.Integer, Mandatory.Integer,
        Mandatory.Integer, _returns=Mandatory.String)
    def sliceChromosome(chromAccNo, start, end, orientation) :
        """
        Todo: documentation, error handling, argument checking, tests.

        @arg orientation: Orientation of the slice. 1 for forward, 2 for
            reverse complement.
        @type orientation: integer
        """
        O = Output(__file__)
        retriever = Retriever.GenBankRetriever(O)

        O.addMessage(__file__, -1, "INFO",
            "Received request sliceChromosome(%s, %s, %s, %s)" % (
            chromAccNo, start, end, orientation))

        UD = retriever.retrieveslice(chromAccNo, start, end, orientation)

        O.addMessage(__file__, -1, "INFO",
            "Finished processing sliceChromosome(%s, %s, %s, %s)" % (
            chromAccNo, start, end, orientation))

        return UD
    #sliceChromosome

    @srpc(_returns=InfoOutput)
    def info():
        """
        Gives some static application information, such as the current running
        version.

        @return: Object with fields:
            - version: A string of the current running version.
            - versionParts: The parts of the current running version as a list
                of strings.
            - releaseDate: The release date for the running version as a
                string, or the empty string in case of a development version.
            - nomenclatureVersion: Version of the HGVS nomenclature used.
            - nomenclatureVersionParts: The parts of the HGVS nomenclature
                version as a list of strings.
            - serverName: The name of the server that is being queried.
            - contactEmail: The email address to contact for more information.
        @rtype: object
        """
        output = Output(__file__)
        output.addMessage(__file__, -1, 'INFO', 'Received request info')

        result = InfoOutput()
        result.version = mutalyzer.__version__
        result.versionParts = mutalyzer.__version_info__
        if mutalyzer.__version_info__[-1] == 'dev':
            result.releaseDate = ''
        else:
            result.releaseDate = mutalyzer.__date__
        result.nomenclatureVersion = mutalyzer.NOMENCLATURE_VERSION
        result.nomenclatureVersionParts = mutalyzer.NOMENCLATURE_VERSION_INFO
        result.serverName = socket.gethostname()
        result.contactEmail = mutalyzer.__contact__

        output.addMessage(__file__, -1, 'INFO', 'Finished processing info')
        return result
    #info

    @srpc(_returns=Mandatory.String)
    def ping():
        """
        Simple function to test the interface.

        @return: Always the value 'pong'.
        @rtype: string
        """
        return 'pong'
    #ping

    @srpc(Mandatory.String, Mandatory.String, _returns=Allele)
    def descriptionExtract(reference, observed):
        """
        Extract the HGVS variant description from a reference sequence and an
        observed sequence.

        Note that this only works on DNA sequences for now.
        """
        output = Output(__file__)

        output.addMessage(__file__, -1, 'INFO',
            'Received request descriptionExtract')

        result = Allele()
        result.allele = describe.describe(reference, observed)
        result.description = describe.alleleDescription(result.allele)

        output.addMessage(__file__, -1, 'INFO',
            'Finished processing descriptionExtract')

        return result
    #descriptionExtract

    @srpc(DateTime, _returns=Array(CacheEntry))
    def getCache(created_since=None):
        """
        Get a list of entries from the local cache created since given date.

        This method is intended to be used by Mutalyzer itself to synchronize
        the cache between installations on different servers.
        """
        output = Output(__file__)

        output.addMessage(__file__, -1, 'INFO', 'Received request getCache')

        sync = CacheSync(output)

        cache = sync.local_cache(created_since)

        def cache_entry_to_soap(entry):
            e = CacheEntry()
            for attr in ('name', 'gi', 'hash', 'chromosomeName',
                'chromosomeStart', 'chromosomeStop', 'chromosomeOrientation',
                'url', 'created', 'cached'):
                setattr(e, attr, entry[attr])
            return e

        output.addMessage(__file__, -1, 'INFO', 'Finished processing getCache')

        return map(cache_entry_to_soap, cache)
    #getCache

    @srpc(Mandatory.String, _returns=Array(Mandatory.String))
    def getdbSNPDescriptions(rs_id):
        """
        Lookup HGVS descriptions for a dbSNP rs identifier.

        @arg rs_id: The dbSNP rs identifier, e.g. 'rs9919552'.
        @type rs_id: string

        @return: List of HGVS descriptions.
        @rtype: list(string)
        """
        output = Output(__file__)

        output.addMessage(__file__, -1, 'INFO',
            'Received request getdbSNPDescription(%s)' % rs_id)

        stats.increment_counter('snp-converter/webservice')

        retriever = Retriever.Retriever(output)
        descriptions = retriever.snpConvert(rs_id)

        output.addMessage(__file__, -1, 'INFO',
            'Finished processing getdbSNPDescription(%s)' % rs_id)

        # Todo: use SOAP Fault object here (see Trac issue #41).
        messages = output.getMessages()
        if messages:
            error = 'The request could not be completed\n' + \
                '\n'.join(map(lambda m: str(m), output.getMessages()))
            raise Exception(error)

        return descriptions
    #getdbSNPDescriptions
#MutalyzerService


# Close database session at end of each call.
def _shutdown_session(ctx):
    session.remove()
MutalyzerService.event_manager.add_listener('method_return_object',
                                            _shutdown_session)
MutalyzerService.event_manager.add_listener('method_exception_object',
                                            _shutdown_session)
