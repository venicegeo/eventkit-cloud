from __future__ import absolute_import

import argparse
import logging
import os
import shutil
import subprocess
from string import Template
from billiard import Process

logger = logging.getLogger(__name__)


class WFSToSQLITE():
    """
    Convert a WFS services to a sqlite file.
    """

    def __init__(self, config=None, sqlite=None, bbox=None, service_url=None, layer=None, debug=None, name=None, service_type=None):
        """
        Initialize the WFSToSQLITE utility.

        Args:
            sqlite: where to write the sqlite output
            debug: turn debugging on / off
        """
        self.sqlite = sqlite
        self.bbox = bbox
        self.service_url = service_url
        self.debug = debug
        self.name = name
        self.layer = layer
        self.config = config
        if self.bbox:
            self.cmd = Template("ogr2ogr -skipfailures -t_srs EPSG:3857 -clipsrc $minX $minY $maxX $maxY -f SQLite $sqlite WFS:'$url'")
        else:
            self.cmd = Template("ogr2ogr -skipfailures -t_srs EPSG:3857 -f SQLite $sqlite WFS:'$url'")

    def convert(self, ):
        """
        Convert wfs to sqlite.
        """
        if not os.path.isdir(os.path.dirname(self.sqlite)):
            os.makedirs(os.path.dirname(self.sqlite), 6600)

        try:
            self.service_url = self.service_url.split('?')[0]
            logger.debug(self.service_url)
        except ValueError:
            self.service_url = self.service_url.rstrip('/\\')
        finally:
            self.service_url = '{}{}'.format(self.service_url, '?SERVICE=WFS&VERSION=1.0.0&REQUEST=GetFeature&TYPENAME={}&SRSNAME=EPSG:4326'.format(self.layer))
        logger.debug(self.service_url)

        if self.bbox:
            logger.debug('using bbox')
            convert_cmd = self.cmd.safe_substitute({'sqlite': self.sqlite, 'url': self.service_url, 'minX': self.bbox[0], 'minY': self.bbox[1], 'maxX': self.bbox[2], 'maxY': self.bbox[3]})
        else:
            convert_cmd = self.cmd.safe_substitute({'sqlite': self.sqlite, 'url': self.service_url})
        logger.debug(convert_cmd)
        if(self.debug):
            logger.debug('Running: %s' % convert_cmd)
        logger.debug('starting process')
        proc = subprocess.Popen(convert_cmd, shell=True, executable='/bin/bash',
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (stdout, stderr) = proc.communicate()
        returncode = proc.wait()

        if (returncode != 0):
            logger.error('%s', stderr)
            raise Exception, "ogr2ogr process failed with returncode {0}".format(returncode)
        if(self.debug):
            logger.debug('ogr2ogr returned: %s' % returncode)
        logger.debug('returning file')
        return self.sqlite
