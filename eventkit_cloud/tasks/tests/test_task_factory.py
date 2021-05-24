# -*- coding: utf-8 -*-
import logging
import os
import uuid

from django.contrib.auth.models import Group, User
from django.contrib.gis.geos import GEOSGeometry, Polygon
from django.db import DatabaseError
from django.test import TestCase
from unittest.mock import patch, Mock

from eventkit_cloud.jobs.models import Job, Region, DataProviderTask, DataProvider, License, UserLicense
from eventkit_cloud.tasks.models import ExportRun
from eventkit_cloud.tasks.task_factory import (
    TaskFactory,
    create_run,
    create_finalize_run_task_collection,
    get_invalid_licenses,
)

logger = logging.getLogger(__name__)


class TestExportTaskFactory(TestCase):
    """
    Test cases for the TaskFactory.
    """

    fixtures = ("osm_provider.json",)

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
        self.job = Job.objects.create(name="TestJob", description="Test description", user=self.user, the_geom=the_geom)
        provider = DataProvider.objects.get(slug="osm")
        self.license = License.objects.create(slug="odbl-test", name="test_osm_license")
        provider.license = self.license
        provider.save()
        UserLicense.objects.create(license=self.license, user=self.user)
        provider_task = DataProviderTask.objects.create(provider=provider)
        self.job.data_provider_tasks.add(provider_task)
        self.region, created = Region.objects.get_or_create(name="Africa", the_geom=the_geom)
        self.job.region = self.region
        self.uid = str(provider_task.uid)
        self.job.save()

    def test_create_run_success(self):
        run_uid = create_run(job_uid=self.job.uid)
        self.assertIsNotNone(run_uid)
        self.assertIsNotNone(ExportRun.objects.get(uid=run_uid))

    @patch("eventkit_cloud.tasks.task_factory.ExportRun")
    def test_create_run_failure(self, ExportRun):
        ExportRun.objects.create.side_effect = DatabaseError("FAIL")
        with self.assertRaises(DatabaseError):
            run_uid = create_run(job_uid=self.job.uid)
            self.assertIsNone(run_uid)

    @patch("eventkit_cloud.tasks.task_factory.os")
    @patch("eventkit_cloud.tasks.task_factory.get_invalid_licenses")
    @patch("eventkit_cloud.tasks.task_factory.create_task")
    @patch("eventkit_cloud.tasks.task_factory.TaskChainBuilder")
    @patch("eventkit_cloud.tasks.task_factory.finalize_export_provider_task")
    @patch("eventkit_cloud.tasks.task_factory.chain")
    def test_task_factory(
        self, task_factory_chain, finalize_task, mock_task_chain_builder, create_task, mock_invalid_licenses, mock_os
    ):
        mock_invalid_licenses.return_value = []
        mock_os.path.exists.return_value = False
        run_uid = create_run(job_uid=self.job.uid)
        self.assertIsNotNone(run_uid)
        self.assertIsNotNone(ExportRun.objects.get(uid=run_uid))
        worker = "some_worker"
        mock_osm_task = "osm-task"
        provider_uuid = uuid.uuid4()
        task = Mock()
        mock_task_chain_builder().build_tasks.return_value = (provider_uuid, task)

        del task.tasks

        task_factory = TaskFactory()
        task_factory.type_task_map = {"osm-generic": mock_osm_task, "osm": mock_osm_task}

        task_factory.parse_tasks(run_uid=run_uid, worker=worker)
        task_factory_chain.assert_called()
        finalize_task.s.assert_called()
        mock_os.makedirs.assert_called()
        self.assertEqual(3, create_task.call_count)

        # Test that run is prevented and deleted if the user has not agreed to the licenses.
        mock_invalid_licenses.return_value = ["invalid-licenses"]
        with self.assertRaises(Exception):
            task_factory.parse_tasks(run_uid=run_uid, worker=worker)
            run = ExportRun.objects.filter(uid=run_uid).first()
            self.assertIsNone(run)

        task.tasks = [task]
        task_factory = TaskFactory()
        task_factory.type_task_map = {"osm": mock_osm_task}

        task_factory.parse_tasks(run_uid=run_uid, worker=worker)
        task_factory_chain.assert_called()

    def test_get_invalid_licenses(self):
        # The license should not be returned if the user has agreed to it.
        expected_invalid_licenses = []
        invalid_licenses = get_invalid_licenses(self.job)
        self.assertEqual(invalid_licenses, expected_invalid_licenses)

        # A license should be returned if the user has not agreed to it.
        UserLicense.objects.get(license=self.license, user=self.user).delete()
        expected_invalid_licenses = [self.license.name]
        invalid_licenses = get_invalid_licenses(self.job)
        self.assertEqual(invalid_licenses, expected_invalid_licenses)

        UserLicense.objects.create(license=self.license, user=self.user)


class CreateFinalizeRunTaskCollectionTests(TestCase):
    @patch("eventkit_cloud.tasks.task_factory.create_zip_task")
    @patch("eventkit_cloud.tasks.task_factory.finalize_run_task")
    @patch("eventkit_cloud.tasks.task_factory.chain")
    def test_create_finalize_run_task_collection(
        self,
        chain,
        finalize_run_task,
        zip_file_task,
    ):
        """Checks that all of the expected tasks were prepared and combined in a chain for return."""
        chain.return_value = "When not mocked, this would be a celery chain"
        # None of these need correspond to real things, they're just to check the inner calls.
        run_uid = 1
        run_dir = "test_dir"
        worker = "test_worker"
        expected_task_settings = {
            "interval": 1,
            "max_retries": 10,
            "queue": worker,
            "routing_key": worker,
            "priority": 70,
        }

        mock_zip_chain = Mock()
        # This should return a chain of tasks ending in the finalize_run_task, plus a task sig for just the
        #    finalize_run_task.
        finalize_chain = create_finalize_run_task_collection(
            run_uid=run_uid, run_dir=run_dir, run_zip_task_chain=mock_zip_chain, apply_args=expected_task_settings
        )

        finalize_run_task.si.assert_called_once_with(run_uid=run_uid, stage_dir=run_dir)
        finalize_run_task.si.return_value.set.assert_called_once_with(**expected_task_settings)

        self.assertEqual(finalize_chain, "When not mocked, this would be a celery chain")
        self.assertEqual(chain.call_count, 1)

        # Grab the args for the first (only) call
        chain_inputs = chain.call_args[0]
        # The result of setting the args & settings for each task,
        # which unmocked would be a task signature, should be passed to celery.chain
        expected_chain_inputs = (mock_zip_chain, finalize_run_task.si.return_value.set.return_value)
        self.assertEqual(chain_inputs, expected_chain_inputs)
