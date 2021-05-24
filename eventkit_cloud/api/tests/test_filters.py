# -*- coding: utf-8 -*-
import logging

from django.contrib.auth.models import Group, User
from django.contrib.gis.geos import GEOSGeometry, Polygon
from unittest.mock import patch
from rest_framework.authtoken.models import Token
from rest_framework.reverse import reverse
from rest_framework.test import APITestCase

from eventkit_cloud.jobs.models import ExportFormat, Job, DataProvider, DataProviderTask, Projection
from eventkit_cloud.tasks.models import ExportRun, DataProviderTaskRecord
from eventkit_cloud.core.models import AttributeClass, attribute_class_filter

logger = logging.getLogger(__name__)


class TestAttributeClassFilter(APITestCase):
    def setUp(
        self,
    ):
        self.user = User.objects.create_user(username="demo1", email="demo@demo.com", password="demo")
        self.attribute_class = AttributeClass.objects.create(name="test1", slug="test1")
        extents = (-3.9, 16.1, 7.0, 27.6)
        bbox = Polygon.from_bbox(extents)
        the_geom = GEOSGeometry(bbox, srid=4326)
        self.data_provider = DataProvider.objects.create(
            name="test1", slug="test1", attribute_class=self.attribute_class
        )
        self.data_providers = DataProvider.objects.all()
        self.job = Job.objects.create(
            name="test1", description="Test description", the_geom=the_geom, user=self.user, json_tags={}
        )
        self.data_provider_task = DataProviderTask.objects.create(provider=self.data_provider)
        self.job.data_provider_tasks.add(self.data_provider_task)
        run = ExportRun.objects.create(job=self.job, user=self.user, status="COMPLETED")
        self.data_provider_task_record = DataProviderTaskRecord.objects.create(
            name=self.data_provider.name, display=True, run=run, status="SUCCESS", provider=self.data_provider
        )

    def check_filter_result(self, queryset):
        returned, excluded = attribute_class_filter(queryset, user=self.user)
        self.assertCountEqual((list(returned), list(excluded)), (list(queryset), list()))

    def check_excluded_result(self, queryset):
        returned, excluded = attribute_class_filter(queryset, user=self.user)
        self.assertCountEqual((list(returned), list(excluded)), (list(), list(queryset)))

    def test_attribute_class_filter(self):

        # First test that objects are returned when user is assigned to the attribute class.
        self.attribute_class.users.add(self.user)
        self.check_filter_result(Job.objects.filter(id=self.job.id))
        self.check_filter_result(DataProvider.objects.filter(id=self.data_provider.id))
        self.check_filter_result(DataProviderTask.objects.filter(id=self.data_provider_task.id))
        self.check_filter_result(DataProviderTaskRecord.objects.filter(id=self.data_provider_task_record.id))

        # Ensure user is not assigned to attribute class.
        self.attribute_class.users.remove(self.user)
        self.check_excluded_result(Job.objects.filter(id=self.job.id))
        self.check_excluded_result(DataProvider.objects.filter(id=self.data_provider.id))
        self.check_excluded_result(DataProviderTask.objects.filter(id=self.data_provider_task.id))
        self.check_excluded_result(DataProviderTaskRecord.objects.filter(id=self.data_provider_task_record.id))


class TestJobFilter(APITestCase):
    fixtures = ("osm_provider.json",)

    def __init__(self, *args, **kwargs):
        self.user1 = None
        self.user2 = None
        self.job1 = None
        self.export_run1 = None
        self.job2 = None
        self.export_run2 = None
        self.job3 = None
        self.export_run3 = None
        super(TestJobFilter, self).__init__(*args, **kwargs)

    def setUp(
        self,
    ):
        extents = (-3.9, 16.1, 7.0, 27.6)
        bbox = Polygon.from_bbox(extents)
        the_geom = GEOSGeometry(bbox, srid=4326)
        group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        with patch("eventkit_cloud.jobs.signals.Group") as mock_group:
            mock_group.objects.get.return_value = group
            self.user1 = User.objects.create_user(username="demo1", email="demo@demo.com", password="demo")
            self.user2 = User.objects.create_user(username="demo2", email="demo@demo.com", password="demo")
        self.job1 = Job.objects.create(
            name="TestJob1", description="Test description", user=self.user1, the_geom=the_geom
        )
        self.job2 = Job.objects.create(
            name="TestJob2", description="Test description", user=self.user2, the_geom=the_geom, featured=True
        )
        self.job3 = Job.objects.create(
            name="TestJob3", description="Test description", user=self.user1, the_geom=the_geom
        )
        self.export_run1 = ExportRun.objects.create(job=self.job1, user=self.user1)
        self.export_run2 = ExportRun.objects.create(job=self.job2, user=self.user2)
        self.export_run3 = ExportRun.objects.create(job=self.job3, user=self.user1)

        export_format = ExportFormat.objects.get(slug="shp")
        export_projection_4326 = Projection.objects.get(srid="4326")
        export_projection_3857 = Projection.objects.get(srid="3857")
        export_provider = DataProvider.objects.get(slug="osm-generic")
        provider_task = DataProviderTask.objects.create(provider=export_provider)
        provider_task.formats.add(export_format)

        self.job1.data_provider_tasks.add(provider_task)
        self.job2.data_provider_tasks.add(provider_task)

        self.job1.projections.add(export_projection_4326)
        self.job2.projections.add(export_projection_4326)
        self.job3.projections.add(export_projection_3857)

        token = Token.objects.create(user=self.user1)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

    def test_filterset_no_user(
        self,
    ):
        url = reverse("api:jobs-list")
        url += "?start=2015-01-01T00:00:00.000000Z&end=2030-08-01T00:00:00.000000Z"
        response = self.client.get(url)
        self.assertEqual(2, len(response.data))

    def test_filterset_with_user(
        self,
    ):
        url = reverse("api:jobs-list")
        url += "?start=2015-01-01T00:00:00.000000Z&end=2030-08-01T00:00:00.000000Z&user=demo1"
        response = self.client.get(url)
        self.assertEqual(2, len(response.data))

    def test_filterset_featured(
        self,
    ):
        url = reverse("api:jobs-list")
        url += "?featured=true"
        response = self.client.get(url)
        self.assertEqual(0, len(response.data))

    def test_filterset_projection_3857(
        self,
    ):
        url = reverse("api:runs-list")
        url += "?projections=3857"
        response = self.client.get(url)
        self.assertEqual(1, len(response.data))
        self.assertEqual(self.job3.name, response.data[0]["job"]["name"])

    def test_filterset_both_projections(
        self,
    ):
        """Expect both export1 and export3 to be found."""
        url = reverse("api:runs-list")
        url += "?projections=3857,4326"
        response = self.client.get(url)
        self.assertEqual(2, len(response.data))

    def test_filterset_projection_4326(
        self,
    ):
        url = reverse("api:runs-list")
        url += "?projections=4326"
        response = self.client.get(url)
        self.assertEqual(1, len(response.data))
        self.assertEqual(self.job1.name, response.data[0]["job"]["name"])
