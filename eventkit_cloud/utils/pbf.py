# -*- coding: utf-8 -*-


import argparse
import logging
import os
import subprocess
from string import Template

from eventkit_cloud.tasks.task_process import TaskProcess

logger = logging.getLogger(__name__)


class OSMToPBF(object):
    """
    Convert OSM to PBF.
    """

    def __init__(self, osm=None, pbffile=None, debug=False, task_uid=None):
        """
        Initialize the OSMToPBF utility.

        Args:
            osm: the raw osm file to convert
            pbffile: the location of the pbf output file
        """
        self.osm = osm
        if not os.path.exists(self.osm):
            raise IOError("Cannot find raw OSM data for this task.")
        self.pbffile = pbffile
        if not self.pbffile:
            # create pbf path from osm path.
            root = self.osm.split(".")[0]
            self.pbffile = root + ".pbf"
        self.debug = debug
        self.cmd = Template("osmconvert $osm --out-pbf >$pbf")
        self.task_uid = task_uid
        try:
            os.remove(self.pbffile)
        except Exception:
            pass

    def convert(
        self,
    ):
        """
        Convert the raw osm to pbf.
        """
        convert_cmd = self.cmd.safe_substitute({"osm": self.osm, "pbf": self.pbffile})
        if self.debug:
            print("Running: %s" % convert_cmd)
        task_process = TaskProcess(task_uid=self.task_uid)
        task_process.start_process(
            convert_cmd,
            shell=True,
            executable="/bin/bash",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if task_process.exitcode != 0:
            logger.error("{0}".format(task_process.stderr))
            logger.error("osmconvert failed with return code: {0}".format(task_process.exitcode))
            logger.error("osmconvert most commonly fails due to lack of memory.")
            raise Exception("Osmconvert Failed.")

        if self.debug:
            print("Osmconvert returned: %s" % task_process.exitcode)
        return self.pbffile


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Converts OSM XML to PBF")
    parser.add_argument("-o", "--osm-file", required=True, dest="osm", help="The OSM file to convert")
    parser.add_argument("-p", "--pbf-file", required=True, dest="pbf", help="The PBF file to write to")
    parser.add_argument("-d", "--debug", action="store_true", help="Turn on debug output")
    args = parser.parse_args()
    config = {}
    for k, v in list(vars(args).items()):
        if v is None:
            continue
        else:
            config[k] = v
    osm = config["osm"]
    pbf = config["pbf"]
    debug = False
    if config.get("debug"):
        debug = True
    o2p = OSMToPBF(osm=osm, pbf=pbf, debug=debug)
    o2p.convert()
