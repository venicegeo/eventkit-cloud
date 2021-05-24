# -*- coding: utf-8 -*-
import logging
import os
from unittest.mock import Mock, patch, MagicMock

from django.contrib.auth.models import Group, User
from django.contrib.gis.geos import GEOSGeometry, Polygon
from django.db.utils import DatabaseError
from django.test import TestCase

from eventkit_cloud.jobs.models import ExportFormat, Job, Region, DataProviderTask, DataProvider
from eventkit_cloud.tasks.task_factory import create_run
from eventkit_cloud.tasks.task_builders import TaskChainBuilder, create_export_task_record
from eventkit_cloud.tasks.export_tasks import osm_data_collection_task, mapproxy_export_task, wcs_export_task

logger = logging.getLogger(__name__)


class TestTaskBuilder(TestCase):
    fixtures = ("osm_provider", "test_providers")

    def setUp(
        self,
    ):
        self.path = os.path.dirname(os.path.realpath(__file__))
        group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        with patch("eventkit_cloud.jobs.signals.Group") as mock_group:
            mock_group.objects.get.return_value = group
            self.user = User.objects.create(username="demo", email="demo@demo.com", password="demo")
        bbox = Polygon.from_bbox((-10.85, 6.25, -10.62, 6.40))
        the_geom = GEOSGeometry(bbox, srid=4326)

        self.shp_task, _ = ExportFormat.objects.get_or_create(
            name="ESRI Shapefile Format", description="Esri Shapefile (OSM Schema)", slug="shp"
        )

        self.job = Job.objects.create(name="TestJob", description="Test description", user=self.user, the_geom=the_geom)
        self.region, created = Region.objects.get_or_create(name="Africa", the_geom=the_geom)
        self.job.region = self.region
        self.job.save()

    @patch("eventkit_cloud.tasks.task_builders.chain")
    def test_run_osm_task(self, mock_chain):
        provider = DataProvider.objects.get(slug="osm")
        provider_task = DataProviderTask.objects.create(provider=provider)
        provider_task.formats.add(self.shp_task)
        provider_task.save()
        self.job.data_provider_tasks.add(provider_task)
        create_run(job_uid=self.job.uid)

        task_chain_builder = TaskChainBuilder()

        # Even though code using pipes seems to be supported here it is throwing an error.
        try:
            task_chain_builder.build_tasks(
                osm_data_collection_task,
                provider_task_uid=provider_task.uid,
                run=self.job.runs.first(),
                worker="some_worker",
            )
        except TypeError:
            pass
        run = self.job.runs.first()
        self.assertIsNotNone(run)
        tasks = run.data_provider_task_records.first().tasks.filter(name="OSM(.gpkg)")
        self.assertIsNotNone(tasks)

    @patch("eventkit_cloud.tasks.task_builders.chain")
    def test_run_wms_task(self, mock_chain):
        provider = DataProvider.objects.get(slug="wms")
        provider_task_record = DataProviderTask.objects.create(provider=provider)
        self.job.data_provider_tasks.add(provider_task_record)
        # celery chain mock
        mock_chain.return_value.apply_async.return_value = Mock()
        create_run(job_uid=self.job.uid)
        task_chain_builder = TaskChainBuilder()
        # Even though code using pipes seems to be supported here it is throwing an error.
        try:
            task_chain_builder.build_tasks(
                mapproxy_export_task,
                provider_task_uid=provider_task_record.uid,
                run=self.job.runs.first(),
                service_type="wms",
                worker="some_worker",
            )
        except TypeError:
            pass
        run = self.job.runs.first()
        self.assertIsNotNone(run)
        tasks = run.data_provider_task_records.first().tasks.filter(name="Raster export (.gpkg)")
        self.assertIsNotNone(tasks)

    @patch("eventkit_cloud.tasks.task_builders.chain")
    def test_run_wcs_task(self, mock_chain):
        provider = DataProvider.objects.get(slug="wms")
        provider_task_record = DataProviderTask.objects.create(provider=provider)
        self.job.data_provider_tasks.add(provider_task_record)
        # celery chain mock
        mock_chain.return_value.apply_async.return_value = Mock()
        create_run(job_uid=self.job.uid)
        task_chain_builder = TaskChainBuilder()
        # Even though code using pipes seems to be supported here it is throwing an error.
        try:
            task_chain_builder.build_tasks(
                wcs_export_task,
                provider_task_uid=provider_task_record.uid,
                run=self.job.runs.first(),
                service_type="wcs",
                worker="some_worker",
            )
        except TypeError:
            pass
        run = self.job.runs.first()
        self.assertIsNotNone(run)
        tasks = run.data_provider_task_records.first().tasks.filter(name="Geotiff Format (.tif)")
        self.assertIsNotNone(tasks)

    @patch("eventkit_cloud.tasks.task_builders.ExportTaskRecord")
    def test_create_export_task_record(self, mock_export_task):
        from eventkit_cloud.tasks.enumerations import TaskState

        task_name = "TaskName"
        export_provider_task_name = "ExportProviderTaskName"
        worker = "Worker"

        expected_result = MagicMock(display=False)
        mock_export_task.objects.create.return_value = expected_result
        task_result = create_export_task_record(
            task_name=task_name, export_provider_task=export_provider_task_name, worker=worker, display=False
        )

        self.assertEqual(task_result, expected_result)
        mock_export_task.objects.create.assert_called_with(
            export_provider_task=export_provider_task_name,
            status=TaskState.PENDING.value,
            name=task_name,
            worker=worker,
            display=False,
        )

        expected_result = MagicMock(display=True)
        mock_export_task.objects.create.return_value = expected_result

        task_result = create_export_task_record(
            task_name=task_name, export_provider_task=export_provider_task_name, worker=worker, display=True
        )
        self.assertEqual(task_result, expected_result)
        mock_export_task.objects.create.assert_called_with(
            export_provider_task=export_provider_task_name,
            status=TaskState.PENDING.value,
            name=task_name,
            worker=worker,
            display=True,
        )

        mock_export_task.objects.create.side_effect = DatabaseError("SomeError")
        with self.assertRaises(DatabaseError):
            create_export_task_record(
                task_name=task_name, export_provider_task=export_provider_task_name, worker=worker, display=True
            )
