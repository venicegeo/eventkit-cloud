# -*- coding: utf-8 -*-

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.contrib.gis.geos import GEOSGeometry, Polygon
from django.template.loader import get_template
from django.test import TestCase
from django.utils import timezone
from collections import OrderedDict

from eventkit_cloud.jobs.models import DataProvider, DataProviderStatus
from eventkit_cloud.jobs.models import Job
from eventkit_cloud.tasks.models import ExportRun
from eventkit_cloud.tasks.scheduled_tasks import (
    expire_runs_task,
    send_warning_email,
    check_provider_availability_task,
    clean_up_queues_task,
    pcf_scale_celery_task,
    list_to_dict,
    get_celery_pcf_task_details,
    order_celery_tasks,
)
from eventkit_cloud.utils.provider_check import CheckResults

import json
import logging
from unittest.mock import patch, call

from notifications.models import Notification

logger = logging.getLogger(__name__)


class TestExpireRunsTask(TestCase):
    def setUp(
        self,
    ):
        group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        with patch("eventkit_cloud.jobs.signals.Group") as mock_group:
            mock_group.objects.get.return_value = group
            self.user = User.objects.create(username="test", email="test@test.com", password="test")
        bbox = Polygon.from_bbox((-10.85, 6.25, -10.62, 6.40))
        the_geom = GEOSGeometry(bbox, srid=4326)
        created_at = timezone.now() - timezone.timedelta(days=7)
        Job.objects.create(
            name="TestJob",
            created_at=created_at,
            published=False,
            description="Test description",
            user=self.user,
            the_geom=the_geom,
        )

    @patch("eventkit_cloud.tasks.scheduled_tasks.send_warning_email")
    def test_expire_runs(self, send_email):
        job = Job.objects.all()[0]
        now_time = timezone.now()
        ExportRun.objects.create(job=job, user=job.user, expiration=now_time + timezone.timedelta(days=8))
        ExportRun.objects.create(job=job, user=job.user, expiration=now_time + timezone.timedelta(days=6))
        ExportRun.objects.create(job=job, user=job.user, expiration=now_time + timezone.timedelta(days=1))
        ExportRun.objects.create(job=job, user=job.user, expiration=now_time - timezone.timedelta(hours=5))

        with patch("eventkit_cloud.tasks.scheduled_tasks.timezone.now") as mock_time:

            mock_time.return_value = now_time

            self.assertEqual("Expire Runs", expire_runs_task.name)
            expire_runs_task.run()
            site_url = getattr(settings, "SITE_URL", "cloud.eventkit.test")
            expected_url = "{0}/status/{1}".format(site_url.rstrip("/"), job.uid)
            send_email.assert_any_call(
                date=now_time + timezone.timedelta(days=1), url=expected_url, addr=job.user.email, job_name=job.name
            )
            send_email.assert_any_call(
                date=now_time + timezone.timedelta(days=6), url=expected_url, addr=job.user.email, job_name=job.name
            )
            self.assertEqual(3, ExportRun.objects.filter(deleted=False).count())
            self.assertEqual(1, ExportRun.objects.filter(deleted=True).count())
            self.assertEqual(2, Notification.objects.all().count())


class TestPcfScaleCeleryTask(TestCase):
    @patch("eventkit_cloud.tasks.scheduled_tasks.get_all_rabbitmq_objects")
    @patch("eventkit_cloud.tasks.scheduled_tasks.order_celery_tasks")
    @patch("eventkit_cloud.tasks.scheduled_tasks.get_celery_pcf_task_details")
    @patch("eventkit_cloud.tasks.scheduled_tasks.PcfClient")
    def test_pcf_scale_celery(
        self, mock_pcf_client, mock_get_celery_pcf_task_details, mock_order_celery_tasks, mock_get_all_rabbitmq_objects
    ):

        # Run until queues are empty.
        example_memory_used = 2048
        example_disk_used = 3072
        mock_pcf_client().get_running_tasks_memory.return_value = example_memory_used
        mock_pcf_client().get_running_tasks.return_value = {"pagination": {"total_results": 1}}
        example_queues = [{"name": "celery", "messages": 2}]
        empty_queues = [{"name": "celery", "messages": 0}]
        mock_get_all_rabbitmq_objects.side_effect = [example_queues, empty_queues]
        celery_task_details = {"task_counts": {"celery": 1}, "memory": example_memory_used, "disk": example_disk_used}
        mock_get_celery_pcf_task_details.return_value = celery_task_details
        ordered_celery_tasks = OrderedDict(
            {
                "queue1": {
                    "command": "celery worker -A eventkit_cloud --loglevel=$LOG_LEVEL -n worker@%h -Q queue1 ",
                    "disk": 2048,
                    "memory": 2048,
                },
                "celery": {
                    "command": "celery worker -A eventkit_cloud --loglevel=$LOG_LEVEL -n celery@%h -Q celery ",
                    "disk": 2048,
                    "memory": 3072,
                    "limit": 2,
                },
            }
        )
        mock_order_celery_tasks.return_value = ordered_celery_tasks
        pcf_scale_celery_task(8000, locking_task_key="A")
        mock_pcf_client().run_task.assert_called_once()
        mock_pcf_client.reset_mock()
        mock_get_all_rabbitmq_objects.side_effect = None

        # Run until memory is exhausted.
        mock_get_all_rabbitmq_objects.return_value = example_queues
        over_memory = 9000
        mock_pcf_client().get_running_tasks_memory.return_value = over_memory
        pcf_scale_celery_task(8000, locking_task_key="B")
        mock_pcf_client().run_task.assert_called_once()
        mock_pcf_client().reset_mock()

        # Don't run if not enough memory.
        mock_get_all_rabbitmq_objects.return_value = example_queues
        mock_get_celery_pcf_task_details.return_value = celery_task_details
        pcf_scale_celery_task(1000, locking_task_key="C")
        mock_pcf_client().run_task.assert_not_called()
        mock_pcf_client().reset_mock()

        # # Don't run if task limit is reached.
        mock_pcf_client().get_running_tasks.return_value = {"pagination": {"total_results": 3}}
        pcf_scale_celery_task(8000, locking_task_key="D")
        mock_pcf_client().run_task.assert_not_called()
        mock_pcf_client().reset_mock()

    @patch("eventkit_cloud.tasks.scheduled_tasks.PcfClient")
    def test_get_celery_pcf_task_details(self, mock_pcf_client):
        # Figure out how to test the two differnt environment variable options
        example_app_name = "example_app_name"
        celery_tasks = ["celery", "group.priority"]
        pcf_task_resources = [{"name": "celery", "memory_in_mb": 2048, "disk_in_mb": 3072}]
        mock_pcf_client.get_running_tasks.return_value = {
            "pagination": {"total_results": 1},
            "resources": pcf_task_resources,
        }
        expected_value = {"task_counts": {"celery": 1, "group.priority": 0}, "memory": 2048, "disk": 3072}

        returned_value = get_celery_pcf_task_details(mock_pcf_client, example_app_name, celery_tasks)
        self.assertEquals(expected_value, returned_value)

    def test_order_celery_tasks(self):
        celery_tasks = {"celery": {}, "group.priority": {}}
        task_counts = {"celery": 1, "group.priority": 0}
        expected_ordered_tasks = OrderedDict({"group.priority": {}, "celery": {}})
        returned_ordered_tasks = order_celery_tasks(celery_tasks, task_counts)
        self.assertEquals(expected_ordered_tasks, returned_ordered_tasks)

    def test_list_to_dict(self):
        example_list = [{"name": "a", "prop": 1}, {"name": "b", "prop": 2}]
        expected_value = {"a": {"name": "a", "prop": 1}, "b": {"name": "b", "prop": 2}}
        returned_value = list_to_dict(example_list, "name")
        self.assertEquals(expected_value, returned_value)


class TestCheckProviderAvailabilityTask(TestCase):
    @patch("eventkit_cloud.utils.provider_check.perform_provider_check")
    def test_check_provider_availability(self, perform_provider_check_mock):
        perform_provider_check_mock.return_value = json.dumps(CheckResults.SUCCESS.value[0])
        first_provider = DataProvider.objects.create(slug="first_provider", name="first_provider")
        second_provider = DataProvider.objects.create(slug="second_provider", name="second_provider")
        DataProviderStatus.objects.create(related_provider=first_provider)

        check_provider_availability_task()

        perform_provider_check_mock.assert_has_calls([call(first_provider, None), call(second_provider, None)])
        statuses = DataProviderStatus.objects.filter(related_provider=first_provider)
        self.assertEqual(len(statuses), 2)
        most_recent_first_provider_status = statuses.order_by("-id")[0]
        self.assertEqual(most_recent_first_provider_status.status, CheckResults.SUCCESS.value[0]["status"])
        self.assertEqual(most_recent_first_provider_status.status_type, CheckResults.SUCCESS.value[0]["type"])
        self.assertEqual(most_recent_first_provider_status.message, CheckResults.SUCCESS.value[0]["message"])


class TestEmailNotifications(TestCase):
    @patch("eventkit_cloud.tasks.scheduled_tasks.EmailMultiAlternatives")
    def test_send_warning_email(self, alternatives):
        now = timezone.now()
        site_url = getattr(settings, "SITE_URL", "http://cloud.eventkit.test")
        url = "{0}/status/1234".format(site_url.rstrip("/"))
        addr = "test@test.com"
        job_name = "job"

        ctx = {"url": url, "date": str(now), "job_name": job_name}

        with self.settings(DEFAULT_FROM_EMAIL="example@eventkit.test"):
            text = get_template("email/expiration_warning.txt").render(ctx)
            html = get_template("email/expiration_warning.html").render(ctx)
            self.assertIsNotNone(html)
            self.assertIsNotNone(text)
            send_warning_email(date=now, url=url, addr=addr, job_name=job_name)
            alternatives.assert_called_once_with(
                "Your EventKit DataPack is set to expire.", text, to=[addr], from_email="example@eventkit.test"
            )
            alternatives().send.assert_called_once()


class TestCleanUpRabbit(TestCase):
    @patch("eventkit_cloud.tasks.scheduled_tasks.delete_rabbit_objects")
    def test_clean_up_queues_task(self, mock_delete_rabbit_objects):
        example_api_url = "https://test/api"
        with self.settings(BROKER_API_URL=example_api_url):
            clean_up_queues_task()
            mock_delete_rabbit_objects.assert_called_once_with(example_api_url)
