# -*- coding: utf-8 -*-
import logging
from uuid import uuid4

from django.test import TransactionTestCase
from unittest.mock import Mock, patch

from eventkit_cloud.utils.pbf import OSMToPBF

logger = logging.getLogger(__name__)


class TestOSMToPBF(TransactionTestCase):
    def setUp(
        self,
    ):
        self.task_process_patcher = patch("eventkit_cloud.utils.pbf.TaskProcess")
        self.task_process = self.task_process_patcher.start()
        self.addCleanup(self.task_process_patcher.stop)
        self.task_uid = uuid4()

    @patch("os.path.exists")
    def test_convert(self, exists):
        osm = "/path/to/sample.osm"
        pbffile = "/path/to/sample.pbf"
        convert_cmd = "osmconvert {0} --out-pbf >{1}".format(osm, pbffile)
        exists.return_value = True
        self.task_process.return_value = Mock(exitcode=0)
        o2p = OSMToPBF(osm=osm, pbffile=pbffile, task_uid=self.task_uid)
        out = o2p.convert()
        self.task_process.assert_called_once_with(task_uid=self.task_uid)
        exists.assert_called_once_with(osm)
        self.task_process().start_process.assert_called_once_with(
            convert_cmd, executable="/bin/bash", shell=True, stderr=-1, stdout=-1
        )
        self.assertEqual(out, pbffile)

        self.task_process.return_value = Mock(exitcode=1)
        with self.assertRaises(Exception):
            o2p.convert()
