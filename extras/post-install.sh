#!/bin/bash

# Post-install script for Mutalyzer. Run after the setuptools installation
# (python setup.py install).
#
# Notice: The definitions in this file are quite specific to the standard
# Mutalyzer environment. This consists of a Debian stable (Squeeze) system
# with Apache and Mutalyzer using its mod_wsgi module. Debian conventions are
# used throughout. See the README file for more information.
#
# Usage (from the source root directory):
#   sudo bash extras/post-install.sh
#
# Todo:
# - Copy doc to /usr/share/doc
# - General cleanup

set -e

# The 'cd /' is a hack to prevent the mutalyzer package under the current
# directory to be used.
PACKAGE_ROOT=$(cd / && python -c 'import mutalyzer; print mutalyzer.package_root()')
BIN_BATCHD=$(which mutalyzer-batchd)
BIN_CACHE_SYNC=$(which mutalyzer-cache-sync)
BIN_UCSC_UPDATE=$(which mutalyzer-ucsc-update)
BIN_WEBSITE=$(which mutalyzer-website.wsgi)
BIN_WEBSERVICE=$(which mutalyzer-webservice.wsgi)

if [ ! -e /etc/mutalyzer/config ]; then
    echo "Creating /etc/mutalyzer/config"
    mkdir -p /etc/mutalyzer
    cp extras/config.example /etc/mutalyzer/config
    chmod -R u=rwX,go=rX /etc/mutalyzer
else
    echo "Not touching /etc/mutalyzer/config (it exists)"
fi

echo "Touching /var/log/mutalyzer.log"
touch /var/log/mutalyzer.log
chown www-data:www-data /var/log/mutalyzer.log
chmod u=rw,go=r /var/log/mutalyzer.log

echo "Touching /var/cache/mutalyzer"
mkdir -p /var/cache/mutalyzer
chown -R www-data:www-data /var/cache/mutalyzer
chmod -R u=rwX,go=rX /var/cache/mutalyzer

echo "Creating /etc/init.d/mutalyzer-batchd"
cp extras/init.d/mutalyzer-batchd /etc/init.d/mutalyzer-batchd
sed -i -e "s@<MUTALYZER_BIN_BATCHD>@${BIN_BATCHD}@g" /etc/init.d/mutalyzer-batchd
chmod u=rwx,go=rx /etc/init.d/mutalyzer-batchd

echo "Installing init script links"
update-rc.d -f mutalyzer-batchd remove
update-rc.d mutalyzer-batchd defaults 98 02

echo "Installing crontab"
cp extras/cron.d/mutalyzer-ucsc-update /etc/cron.d/mutalyzer-ucsc-update
sed -i -e "s@<MUTALYZER_BIN_UCSC_UPDATE>@${BIN_UCSC_UPDATE}@g" /etc/cron.d/mutalyzer-ucsc-update
cp extras/cron.d/mutalyzer-cache-sync /etc/cron.d/mutalyzer-cache-sync
sed -i -e "s@<MUTALYZER_BIN_CACHE_SYNC>@${BIN_CACHE_SYNC}@g" /etc/cron.d/mutalyzer-cache-sync

echo "Creating /etc/apache2/conf.d/mutalyzer.conf"
cp extras/apache/mutalyzer.conf /etc/apache2/conf.d/mutalyzer.conf
sed -i -e "s@<MUTALYZER_BIN_WEBSITE>@${BIN_WEBSITE}@g" -e "s@<MUTALYZER_BIN_WEBSERVICE>@${BIN_WEBSERVICE}@g" -e "s@<MUTALYZER_BIN_BATCHD>@${BIN_BATCHD}@g" /etc/apache2/conf.d/mutalyzer.conf
chmod u=rw,go=r /etc/apache2/conf.d/mutalyzer.conf

echo "You will now be asked for the MySQL root password"

# Create databases
cat << EOF | mysql -u root -p
  CREATE USER mutalyzer;
  CREATE DATABASE mutalyzer;
  CREATE DATABASE hg18;
  CREATE DATABASE hg19;
  GRANT ALL PRIVILEGES ON mutalyzer.* TO mutalyzer;
  GRANT ALL PRIVILEGES ON hg18.* TO mutalyzer;
  GRANT ALL PRIVILEGES ON hg19.* TO mutalyzer;
  FLUSH PRIVILEGES;
EOF

mkdir -p /tmp/mutalyzer-install
pushd /tmp/mutalyzer-install

# Do hg18
mkdir -p hg18
pushd hg18

echo "Creating and populating hg18 database"

# Then retrieve the refLink table from the UCSC website (hg18)
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg18/database/refLink.sql
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg18/database/refLink.txt.gz

# For Variant_info to work, you need the following files too (hg18)
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg18/database/refGene.sql
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg18/database/refGene.txt.gz
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg18/database/gbStatus.sql
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg18/database/gbStatus.txt.gz

# Create table and load data (hg18)
mysql -u mutalyzer -D hg18 < refLink.sql
zcat refLink.txt.gz | mysql -u mutalyzer -D hg18 -e 'LOAD DATA LOCAL INFILE "/dev/stdin" INTO TABLE refLink;'

mysql -u mutalyzer -D hg18 < gbStatus.sql
zgrep mRNA gbStatus.txt.gz > gbStatus.mrna.txt
mysql -u mutalyzer -D hg18 -e 'LOAD DATA LOCAL INFILE "gbStatus.mrna.txt" INTO TABLE gbStatus;'

mysql -u mutalyzer -D hg18 < refGene.sql
zcat refGene.txt.gz | mysql -u mutalyzer -D hg18 -e 'LOAD DATA LOCAL INFILE "/dev/stdin" INTO TABLE refGene;'

# Combine the mapping info into one table (hg18)
cat << EOF | mysql -u mutalyzer -D hg18
  CREATE TABLE map
    SELECT DISTINCT acc, version, txStart, txEnd, cdsStart, cdsEnd,
                    exonStarts, exonEnds, name2 AS geneName, chrom,
                    strand, protAcc
    FROM gbStatus, refGene, refLink
    WHERE type = "mRNA"
    AND refGene.name = acc
    AND acc = mrnaAcc;
  CREATE TABLE map_cdsBackup (
    acc char(12) NOT NULL DEFAULT '',
    version smallint(6) unsigned NOT NULL DEFAULT '0',
    txStart int(11) unsigned NOT NULL DEFAULT '0',
    txEnd int(11) unsigned NOT NULL DEFAULT '0',
    cdsStart int(11) unsigned NOT NULL DEFAULT '0',
    cdsEnd int(11) unsigned NOT NULL DEFAULT '0',
    exonStarts longblob NOT NULL,
    exonEnds longblob NOT NULL,
    geneName varchar(255) NOT NULL DEFAULT '',
    chrom varchar(255) NOT NULL DEFAULT '',
    strand char(1) NOT NULL DEFAULT '',
    protAcc varchar(255) NOT NULL DEFAULT ''
  );
EOF

popd
rm -Rf /tmp/mutalyzer-install/hg18

# Do hg19
mkdir -p hg19
pushd hg19

echo "Creating and populating hg19 database"

# Then retrieve the refLink table from the UCSC website (hg19)
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg19/database/refLink.sql
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg19/database/refLink.txt.gz

# For Variant_info to work, you need the following files too (hg19)
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg19/database/refGene.sql
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg19/database/refGene.txt.gz
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg19/database/gbStatus.sql
wget http://hgdownload.cse.ucsc.edu/goldenPath/hg19/database/gbStatus.txt.gz

# Create table and load data (hg19)
mysql -u mutalyzer -D hg19 < refLink.sql
zcat refLink.txt.gz | mysql -u mutalyzer -D hg19 -e 'LOAD DATA LOCAL INFILE "/dev/stdin" INTO TABLE refLink;'

mysql -u mutalyzer -D hg19 < gbStatus.sql
zgrep mRNA gbStatus.txt.gz > gbStatus.mrna.txt
mysql -u mutalyzer -D hg19 -e 'LOAD DATA LOCAL INFILE "gbStatus.mrna.txt" INTO TABLE gbStatus;'

mysql -u mutalyzer -D hg19 < refGene.sql
zcat refGene.txt.gz | mysql -u mutalyzer -D hg19 -e 'LOAD DATA LOCAL INFILE "/dev/stdin" INTO TABLE refGene;'

# Combine the mapping info into one table (hg19)
cat << EOF | mysql -u mutalyzer -D hg19
  CREATE TABLE map
    SELECT DISTINCT acc, version, txStart, txEnd, cdsStart, cdsEnd,
                    exonStarts, exonEnds, name2 AS geneName, chrom,
                    strand, protAcc
    FROM gbStatus, refGene, refLink
    WHERE type = "mRNA"
    AND refGene.name = acc
    AND acc = mrnaAcc;
  CREATE TABLE map_cdsBackup (
    acc char(12) NOT NULL DEFAULT '',
    version smallint(5) unsigned NOT NULL DEFAULT '0',
    txStart int(10) unsigned NOT NULL DEFAULT '0',
    txEnd int(10) unsigned NOT NULL DEFAULT '0',
    cdsStart int(10) unsigned NOT NULL DEFAULT '0',
    cdsEnd int(10) unsigned NOT NULL DEFAULT '0',
    exonStarts longblob NOT NULL,
    exonEnds longblob NOT NULL,
    geneName varchar(255) NOT NULL DEFAULT '',
    chrom varchar(255) NOT NULL DEFAULT '',
    strand char(1) NOT NULL DEFAULT '',
    protAcc varchar(255) NOT NULL DEFAULT ''
  );
EOF

popd

popd
rm -Rf /tmp/mutalyzer-install

# Create ChrName tables (hg18)
cat << EOF | mysql -u mutalyzer -D hg18
CREATE TABLE ChrName (
  AccNo char(20) NOT NULL,
  name char(20) NOT NULL,
  PRIMARY KEY (AccNo)
);
INSERT INTO ChrName (AccNo, name) VALUES
('NC_000001.9', 'chr1'),
('NC_000002.10', 'chr2'),
('NC_000003.10', 'chr3'),
('NC_000004.10', 'chr4'),
('NC_000005.8', 'chr5'),
('NC_000006.10', 'chr6'),
('NC_000007.12', 'chr7'),
('NC_000008.9', 'chr8'),
('NC_000009.10', 'chr9'),
('NC_000010.9', 'chr10'),
('NC_000011.8', 'chr11'),
('NC_000012.10', 'chr12'),
('NC_000013.9', 'chr13'),
('NC_000014.7', 'chr14'),
('NC_000015.8', 'chr15'),
('NC_000016.8', 'chr16'),
('NC_000017.9', 'chr17'),
('NC_000018.8', 'chr18'),
('NC_000019.8', 'chr19'),
('NC_000020.9', 'chr20'),
('NC_000021.7', 'chr21'),
('NC_000022.9', 'chr22'),
('NC_000023.9', 'chrX'),
('NC_000024.8', 'chrY'),
('NC_001807.4', 'chrM'),
('NT_113891.1', 'chr6_cox_hap1'),
('NT_113959.1', 'chr22_h2_hap1');
EOF

# Create ChrName tables (hg19)
cat << EOF | mysql -u mutalyzer -D hg19
CREATE TABLE ChrName (
  AccNo char(20) NOT NULL,
  name char(20) NOT NULL,
  PRIMARY KEY (AccNo)
);
INSERT INTO ChrName (AccNo, name) VALUES
('NC_000001.10', 'chr1'),
('NC_000002.11', 'chr2'),
('NC_000003.11', 'chr3'),
('NC_000004.11', 'chr4'),
('NC_000005.9', 'chr5'),
('NC_000006.11', 'chr6'),
('NC_000007.13', 'chr7'),
('NC_000008.10', 'chr8'),
('NC_000009.11', 'chr9'),
('NC_000010.10', 'chr10'),
('NC_000011.9', 'chr11'),
('NC_000012.11', 'chr12'),
('NC_000013.10', 'chr13'),
('NC_000014.8', 'chr14'),
('NC_000015.9', 'chr15'),
('NC_000016.9', 'chr16'),
('NC_000017.10', 'chr17'),
('NC_000018.9', 'chr18'),
('NC_000019.9', 'chr19'),
('NC_000020.10', 'chr20'),
('NC_000021.8', 'chr21'),
('NC_000022.10', 'chr22'),
('NC_000023.10', 'chrX'),
('NC_000024.9', 'chrY'),
('NT_167244.1', 'chr6_apd_hap1'),
('NT_113891.2', 'chr6_cox_hap2'),
('NT_167245.1', 'chr6_dbb_hap3'),
('NT_167246.1', 'chr6_mann_hap4'),
('NT_167247.1', 'chr6_mcf_hap5'),
('NT_167248.1', 'chr6_qbl_hap6'),
('NT_167249.1', 'chr6_ssto_hap7'),
('NT_167250.1', 'chr4_ctg9_hap1'),
('NT_167251.1', 'chr17_ctg5_hap1');
EOF

echo "Creating tables in mutalyzer database"

# Create mutalyzer tables
cat << EOF | mysql -u mutalyzer -D mutalyzer
CREATE TABLE BatchJob (
  JobID char(20) NOT NULL,
  Filter char(20) NOT NULL,
  EMail char(255) NOT NULL,
  FromHost char(255) NOT NULL,
  JobType char(20) DEFAULT NULL,
  Arg1 char(20) DEFAULT NULL,
  PRIMARY KEY (JobID)
);
CREATE TABLE BatchQueue (
  QueueID int(5) NOT NULL AUTO_INCREMENT,
  JobID char(20) NOT NULL,
  Input char(255) NOT NULL,
  Flags char(20) DEFAULT NULL,
  PRIMARY KEY (QueueID),
  KEY JobQueue (JobID,QueueID)
);
CREATE TABLE GBInfo (
  AccNo char(20) NOT NULL DEFAULT '',
  GI char(13) DEFAULT NULL,
  hash char(32) NOT NULL DEFAULT '',
  ChrAccVer char(20) DEFAULT NULL,
  ChrStart int(12) DEFAULT NULL,
  ChrStop int(12) DEFAULT NULL,
  orientation int(2) DEFAULT NULL,
  url char(255) DEFAULT NULL,
  created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (AccNo),
  UNIQUE KEY hash (hash),
  UNIQUE KEY alias (GI),
  INDEX (created)
);
CREATE TABLE Link (
  mrnaAcc char(20) NOT NULL,
  protAcc char(20) NOT NULL,
  PRIMARY KEY (mrnaAcc),
  UNIQUE KEY protAcc (protAcc)
);
CREATE TABLE mm1 (
  hg18 char(50) DEFAULT NULL,
  hg19 char(50) DEFAULT NULL
);
CREATE TABLE mm2 (
  hg18 char(50) DEFAULT NULL,
  hg19 char(50) DEFAULT NULL
);
EOF

# The remainder is essentially the same as post-upgrade.sh

if [ ! -e /var/www/mutalyzer ]; then
    mkdir -p /var/www/mutalyzer
fi

if [ -e /var/www/mutalyzer/base ]; then
    echo "Removing /var/www/mutalyzer/base"
    rm /var/www/mutalyzer/base
fi

echo "Symlinking /var/www/mutalyzer/base to $PACKAGE_ROOT/templates/base"
ln -s $PACKAGE_ROOT/templates/base /var/www/mutalyzer/base

echo "Restarting Apache"
/etc/init.d/apache2 restart

echo "Restarting Mutalyzer batch daemon"
/etc/init.d/mutalyzer-batchd restart
