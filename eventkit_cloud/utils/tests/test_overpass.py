# -*- coding: utf-8 -*-
import logging
import os

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.contrib.gis.geos import GEOSGeometry, Polygon
from django.test import TestCase
from unittest.mock import patch, Mock
import yaml

from eventkit_cloud.jobs.models import ExportFormat, Job, DatamodelPreset
from eventkit_cloud.utils.overpass import Overpass

logger = logging.getLogger(__name__)


class TestOverpass(TestCase):
    fixtures = ("datamodel_presets.json",)

    def setUp(
        self,
    ):
        self.url = settings.OVERPASS_API_URL
        self.bbox = [-10.85, 6.25, -10.62, 6.4]  # [<long0>, <lat0>, <long1>, <lat1>]
        self.path = os.path.dirname(os.path.realpath(__file__))
        self.formats = ExportFormat.objects.all()  # pre-loaded by 'insert_export_formats' migration
        group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        with patch("eventkit_cloud.jobs.signals.Group") as mock_group:
            mock_group.objects.get.return_value = group
            self.user = User.objects.create(username="demo", email="demo@demo.com", password="demo")
        bbox = Polygon.from_bbox((-7.96, 22.6, -8.14, 27.12))
        the_geom = GEOSGeometry(bbox, srid=4326)

        preset = DatamodelPreset.objects.get(name="hdm")
        tags = preset.json_tags
        self.assertEqual(259, len(tags))

        self.job = Job.objects.create(
            name="TestJob",
            description="Test description",
            event="Nepal activation",
            user=self.user,
            the_geom=the_geom,
            json_tags=tags,
        )
        self.uid = self.job.uid
        # add the formats to the job
        self.job.formats = self.formats
        self.job.save()
        self.osm = self.path + "/files/query.osm"
        self.query = (
            "[maxsize:2147483648][timeout:1600];relation(6.25,-10.85,6.4,-10.62);way(6.25,-10.85,6.4,-10.62);"
            "node(6.25,-10.85,6.4,-10.62);<;(._;>;);out body;"
        )

    def test_get_query(
        self,
    ):
        overpass = Overpass(stage_dir=self.path + "/files/", bbox=self.bbox, job_name="testjob")
        q = overpass.get_query()
        self.assertEqual(q, self.query)

    def test_custom_query(
        self,
    ):
        example_max_size = 10000
        example_timeout = 10000
        example_template = "$maxsize-$timeout-$bbox"
        expected_query = (
            f"{example_max_size}-{example_timeout}-{self.bbox[1]},{self.bbox[0]},{self.bbox[3]},{self.bbox[2]}"
        )
        config = yaml.dump({"overpass_query": example_template})
        with self.settings(OVERPASS_MAX_SIZE=example_max_size, OVERPASS_TIMEOUT=example_timeout):
            overpass = Overpass(stage_dir=self.path + "/files/", bbox=self.bbox, job_name="testjob", config=config)
        query = overpass.get_query()
        self.assertEqual(query, expected_query)

    @patch("django.db.connection.close")
    @patch("eventkit_cloud.tasks.models.ExportTaskRecord")
    @patch("eventkit_cloud.utils.overpass.auth_requests.post")
    def test_run_query(self, mock_post, export_task, mock_close):
        verify_ssl = getattr(settings, "SSL_VERIFICATION", True)
        export_task_instance = Mock(progress=0, estimated_finish=None)
        export_task.objects.get.return_value = export_task_instance
        # Only important that it's not None
        mock_export_task_instance_id = 1
        op = Overpass(
            stage_dir=self.path + "/files/",
            task_uid=mock_export_task_instance_id,
            bbox=self.bbox,
            job_name="testjob",
            slug="testslug",
        )
        overpass_query = op.get_query()
        out = self.path + "/files/query.osm"
        mock_response = Mock()
        sample_data = ["<osm>some data</osm>".encode()]
        expected = sample_data[0].decode()
        mock_response.headers = {"content-length": 20}
        mock_response.iter_content.return_value = sample_data
        mock_post.return_value = mock_response
        op.run_query()
        mock_post.assert_called_once_with(self.url, cert_info=None, data=overpass_query, stream=True, verify=verify_ssl)
        mock_close.assert_called()
        f = open(out)
        data = f.read()
        self.assertEqual(data, expected)
        f.close()
        os.remove(out)
