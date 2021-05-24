# -*- coding: utf-8 -*-
import json
import logging
import os
import uuid
import yaml
from datetime import datetime, timedelta
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.contrib.gis.gdal import DataSource
from django.contrib.gis.geos import GEOSGeometry, GeometryCollection, Polygon, Point, LineString
from django.core import serializers
from django.utils import timezone
from unittest.mock import patch, Mock
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.reverse import reverse
from rest_framework.serializers import ValidationError
from rest_framework.test import APITestCase
from eventkit_cloud.api.pagination import LinkHeaderPagination
from eventkit_cloud.api.views import get_models, get_provider_task, ExportRunViewSet
from eventkit_cloud.core.models import GroupPermission, GroupPermissionLevel, AttributeClass
from eventkit_cloud.jobs.admin import get_example_from_file
from eventkit_cloud.jobs.models import (
    ExportFormat,
    Job,
    DataProvider,
    DataProviderType,
    DataProviderTask,
    Region,
    RegionalJustification,
    RegionalPolicy,
    bbox_to_geojson,
    DatamodelPreset,
    License,
    VisibilityState,
    UserJobActivity,
)
from eventkit_cloud.tasks.enumerations import TaskState
from eventkit_cloud.tasks.models import (
    DataProviderTaskRecord,
    ExportRun,
    ExportTaskRecord,
    FileProducingTaskResult,
    RunZipFile,
)
from eventkit_cloud.tasks.task_factory import InvalidLicense
from eventkit_cloud.user_requests.models import DataProviderRequest, SizeIncreaseRequest

logger = logging.getLogger(__name__)


def add_max_data_size(provider, max_data_size):
    config = provider.config
    config = yaml.load(config)
    config["max_data_size"] = max_data_size
    provider.config = yaml.dump(config)
    provider.save()


class TestJobViewSet(APITestCase):
    fixtures = (
        "osm_provider.json",
        "datamodel_presets.json",
    )

    def __init__(self, *args, **kwargs):
        super(TestJobViewSet, self).__init__(*args, **kwargs)
        self.path = None
        self.group = None
        self.user = None
        self.job = None
        self.client = None
        self.config = None
        self.tags = None

    @patch("eventkit_cloud.api.views.get_estimate_cache_key")
    @patch("eventkit_cloud.api.views.cache")
    def setUp(self, cache_mock, get_estimate_cache_key_mock):
        self.path = os.path.dirname(os.path.realpath(__file__))
        self.group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo")
        self.attribute_class = AttributeClass.objects.create(name="test", slug="test", filter="username=demo")
        self.attribute_class.users.add(self.user)
        self.attribute_class.save()
        extents = (-3.9, 16.1, 7.0, 27.6)
        bbox = Polygon.from_bbox(extents)
        original_selection = GeometryCollection(Point(1, 1), LineString((5.625, 48.458), (0.878, 44.339)))
        the_geom = GEOSGeometry(bbox, srid=4326)
        self.job = Job.objects.create(
            name="TestJob",
            event="Test Activation",
            description="Test description",
            user=self.user,
            the_geom=the_geom,
            original_selection=original_selection,
        )

        formats = ExportFormat.objects.all()
        self.provider = DataProvider.objects.first()
        self.provider.attribute_class = self.attribute_class
        self.provider.save()
        provider_task = DataProviderTask.objects.create(provider=self.provider, job=self.job)
        provider_task.formats.add(*formats)

        token = Token.objects.create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )
        hdm_presets = DatamodelPreset.objects.get(name="hdm")
        self.job.preset = hdm_presets
        self.job.save()

        self.tags = [
            {
                "name": "Telecommunication office",
                "key": "office",
                "value": "telecommunication",
                "geom": ["point", "polygon"],
            },
            {"name": "Radio or TV Studio", "key": "amenity", "value": "studio", "geom": ["point", "polygon"]},
            {"name": "Telecommunication antenna", "key": "man_made", "value": "tower", "geom": ["point", "polygon"]},
            {
                "name": "Telecommunication company retail office",
                "key": "office",
                "value": "telecommunication",
                "geom": ["point", "polygon"],
            },
        ]

        self.estimate_size = 110
        self.estimate_time = 200
        cache_mock.get.return_value = [self.estimate_size, self.estimate_time]
        self.cache_key = "222222222222222"
        get_estimate_cache_key_mock.return_value = self.cache_key

    def test_list(
        self,
    ):
        expected = "/api/jobs"
        url = reverse("api:jobs-list")
        self.assertEqual(expected, url)

    def test_make_job_with_export_providers(
        self,
    ):
        """tests job creation with export providers"""
        export_providers = DataProvider.objects.all()
        export_providers_start_len = len(export_providers)
        formats = [export_format.slug for export_format in ExportFormat.objects.all()]

        request_data = {
            "name": "TestJob",
            "description": "Test description",
            "event": "Test Activation",
            "selection": bbox_to_geojson([5, 16, 5.1, 16.1]),
            "provider_tasks": [{"provider": "test", "formats": formats, "min_zoom": 3, "max_zoom": 6}],
            "export_providers": [
                {
                    "name": "test",
                    "level_from": 0,
                    "level_to": 1,
                    "url": "http://coolproviderurl.to",
                    "preview_url": "http://coolproviderurl.to",
                }
            ],
            "user": serializers.serialize("json", [self.user]),
            "preset": self.job.preset.id,
        }
        url = reverse("api:jobs-list")
        response = self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        response = response.json()
        export_providers = DataProvider.objects.all()

        # Test that the provider task was created with custom zoom levels.
        provider_task = DataProviderTask.objects.last()
        self.assertEqual(provider_task.min_zoom, request_data["provider_tasks"][0]["min_zoom"])
        self.assertEqual(provider_task.max_zoom, request_data["provider_tasks"][0]["max_zoom"])

        self.assertEqual(len(export_providers), export_providers_start_len + 1)

        self.assertEqual(response["exports"][0]["provider"], "test")

        request_data["export_providers"][0]["name"] = "test 2"
        # should be idempotent
        self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")

        export_providers = DataProvider.objects.all()
        self.assertEqual(len(export_providers), export_providers_start_len + 1)

    def test_get_job_detail(
        self,
    ):
        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        self.assertEqual(expected, url)
        data = {
            "uid": str(self.job.uid),
            "name": "TestJob",
            "url": "http://testserver{0}".format(url),
            "description": "Test Description",
            "exports": [
                {
                    "provider": "osm-generic",
                    "formats": [
                        {
                            "uid": "8611792d-3d99-4c8f-a213-787bc7f3066",
                            "url": "http://testserver/api/formats/gpkg",
                            "name": "Geopackage",
                            "description": "Geopackage",
                        }
                    ],
                }
            ],
            "created_at": "2015-05-21T19:46:37.163749Z",
            "updated_at": "2015-05-21T19:46:47.207111Z",
            "status": "SUCCESS",
        }
        response = self.client.get(url)
        # test the response headers
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")

        # test significant content
        self.assertEqual(response.data["uid"], data["uid"])
        self.assertEqual(response.data["url"], data["url"])
        self.assertEqual(response.data["exports"][0]["formats"][0]["url"], data["exports"][0]["formats"][0]["url"])

    def test_get_job_detail_no_attribute_class(
        self,
    ):
        self.attribute_class.users.remove(self.user)
        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        self.assertEqual(expected, url)
        data = {"uid": str(self.job.uid), "provider_task_list_status": "EMPTY", "provider_tasks": []}
        response = self.client.get(url)
        # test the response headers
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # test significant content
        self.assertEqual(response.data["provider_task_list_status"], data["provider_task_list_status"])
        self.assertEqual(response.data["provider_tasks"], data["provider_tasks"])

    def test_get_job_detail_no_permissions(
        self,
    ):
        user = User.objects.create_user(username="demo2", email="demo2@demo.com", password="demo")
        token = Token.objects.create(user=user)
        # reset the client credentials to the new user
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )
        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        self.assertEqual(expected, url)
        response = self.client.get(url)
        # test the response headers
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")

        # test significant content
        self.assertIsNotNone(response.data["errors"][0]["detail"])

    def test_delete_job(
        self,
    ):
        url = reverse("api:jobs-detail", args=[self.job.uid])
        response = self.client.delete(url)
        # test the response headers
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(response["Content-Length"], "0")
        self.assertEqual(response["Content-Language"], "en")

    def test_delete_job_no_permission(
        self,
    ):
        user = User.objects.create_user(username="demo2", email="demo2@demo.com", password="demo")
        token = Token.objects.create(user=user)
        # reset the client credentials to the new user
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

        url = reverse("api:jobs-detail", args=[self.job.uid])
        response = self.client.delete(url)
        # test the response headers
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["errors"][0]["detail"], "ADMIN permission is required to delete this job.")

    @patch("eventkit_cloud.api.views.pick_up_run_task")
    @patch("eventkit_cloud.api.views.create_run")
    def test_create_zipfile(self, create_run_mock, pickup_mock):
        bbox = (5, 16, 5.1, 16.1)
        max_zoom = 17
        min_zoom = 0

        create_run_mock.return_value = "some_run_uid"
        formats = [export_format.slug for export_format in ExportFormat.objects.all()]
        request_data = {
            "name": "TestJob",
            "description": "Test description",
            "event": "Test Activation",
            "selection": bbox_to_geojson(bbox),
            "provider_tasks": [
                {"provider": self.provider.slug, "formats": formats, "min_zoom": min_zoom, "max_zoom": max_zoom}
            ],
            "preset": self.job.preset.id,
            "published": True,
            "tags": self.tags,
            "include_zipfile": True,
        }

        url = reverse("api:jobs-list")
        response = self.client.post(url, request_data, format="json")
        expected_user_details = {"username": "demo", "is_superuser": False, "is_staff": False}
        pickup_mock.apply_async.assert_called_with(
            kwargs={"run_uid": "some_run_uid", "user_details": expected_user_details, "session_token": None},
            queue="runs",
            routing_key="runs",
        )
        msg = "status_code {} != {}: {}".format(200, response.status_code, response.content)
        self.assertEqual(202, response.status_code, msg)
        job_uid = response.data["uid"]

        job = Job.objects.get(uid=job_uid)
        self.assertEqual(job.include_zipfile, True)

    @patch("eventkit_cloud.api.views.pick_up_run_task")
    @patch("eventkit_cloud.api.views.create_run")
    def test_create_job_success(self, create_run_mock, pickup_mock):
        bbox = (5, 16, 5.1, 16.1)
        max_zoom = 17
        min_zoom = 0

        create_run_mock.return_value = "some_run_uid"
        url = reverse("api:jobs-list")
        logger.debug(url)
        formats = [export_format.slug for export_format in ExportFormat.objects.all()]
        request_data = {
            "name": "TestJob",
            "description": "Test description",
            "event": "Test Activation",
            "selection": bbox_to_geojson(bbox),
            "provider_tasks": [
                {"provider": self.provider.slug, "formats": formats, "min_zoom": min_zoom, "max_zoom": max_zoom}
            ],
            "preset": self.job.preset.id,
            "published": True,
            "tags": self.tags,
            "original_selection": {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 1]}},
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": [[5.625, 48.458], [0.878, 44.339]]},
                    },
                ],
            },
        }
        response = self.client.post(url, request_data, format="json")
        job_uid = response.data["uid"]
        # test that the mock methods get called.
        create_run_mock.assert_called_once_with(job_uid=job_uid, user=self.user)
        expected_user_details = {"username": "demo", "is_superuser": False, "is_staff": False}
        pickup_mock.apply_async.assert_called_once_with(
            kwargs={"run_uid": "some_run_uid", "user_details": expected_user_details, "session_token": None},
            queue="runs",
            routing_key="runs",
        )
        # test the response headers
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")

        # test significant response content

        self.assertEqual(
            response.data["exports"][0]["formats"][0]["slug"], request_data["provider_tasks"][0]["formats"][0]
        )
        self.assertEqual(
            response.data["exports"][0]["formats"][1]["slug"], request_data["provider_tasks"][0]["formats"][1]
        )
        self.assertEqual(response.data["name"], request_data["name"])
        self.assertEqual(response.data["description"], request_data["description"])
        self.assertEqual(response.data["original_selection"], request_data["original_selection"])
        self.assertTrue(response.data["published"])

        # check we have the correct tags
        job = Job.objects.get(uid=job_uid)
        self.assertIsNotNone(job.preset.json_tags)
        self.assertEqual(259, len(job.preset.json_tags))

    @patch("eventkit_cloud.api.views.pick_up_run_task")
    @patch("eventkit_cloud.api.views.create_run")
    def test_create_job_with_config_success(self, create_run_mock, pickup_mock):
        bbox = (5, 16, 5.1, 16.1)
        max_zoom = 17
        min_zoom = 0

        create_run_mock.return_value = "some_run_uid"
        url = reverse("api:jobs-list")
        formats = [export_format.slug for export_format in ExportFormat.objects.all()]
        request_data = {
            "name": "TestJob",
            "description": "Test description",
            "event": "Test Activation",
            "selection": bbox_to_geojson(bbox),
            "provider_tasks": [
                {"provider": self.provider.slug, "formats": formats, "min_zoom": min_zoom, "max_zoom": max_zoom}
            ],
            "preset": self.job.preset.id,
            "transform": "",
            "translation": "",
        }
        response = self.client.post(url, request_data, format="json")

        job_uid = response.data["uid"]
        # test that the mock methods get called.
        create_run_mock.assert_called_once_with(job_uid=job_uid, user=self.user)
        expected_user_details = {"username": "demo", "is_superuser": False, "is_staff": False}
        pickup_mock.apply_async.assert_called_once_with(
            kwargs={"run_uid": "some_run_uid", "user_details": expected_user_details, "session_token": None},
            queue="runs",
            routing_key="runs",
        )

        # test the response headers
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")

        # test significant response content
        self.assertEqual(
            response.data["exports"][0]["formats"][0]["slug"], request_data["provider_tasks"][0]["formats"][0]
        )
        self.assertEqual(
            response.data["exports"][0]["formats"][1]["slug"], request_data["provider_tasks"][0]["formats"][1]
        )
        self.assertEqual(response.data["name"], request_data["name"])
        self.assertEqual(response.data["description"], request_data["description"])
        self.assertFalse(response.data["published"])
        self.assertEqual(259, len(self.job.preset.json_tags))

    @patch("eventkit_cloud.api.views.pick_up_run_task")
    @patch("eventkit_cloud.api.views.create_run")
    def test_create_job_with_tags(self, create_run_mock, pickup_mock):
        bbox = (5, 16, 5.1, 16.1)
        max_zoom = 17
        min_zoom = 0

        create_run_mock.return_value = "some_run_uid"

        url = reverse("api:jobs-list")
        formats = [export_format.slug for export_format in ExportFormat.objects.all()]
        request_data = {
            "name": "TestJob",
            "description": "Test description",
            "event": "Test Activation",
            "selection": bbox_to_geojson(bbox),
            "provider_tasks": [
                {"provider": "osm-generic", "formats": formats, "min_zoom": min_zoom, "max_zoom": max_zoom}
            ],
            "preset": self.job.preset.id,
            "transform": "",
            "translate": "",
            "tags": self.tags,
        }
        response = self.client.post(url, request_data, format="json")
        job_uid = response.data["uid"]
        # test that the mock methods get called.
        create_run_mock.assert_called_once_with(job_uid=job_uid, user=self.user)
        expected_user_details = {"username": "demo", "is_superuser": False, "is_staff": False}
        pickup_mock.apply_async.assert_called_once_with(
            kwargs={"run_uid": "some_run_uid", "user_details": expected_user_details, "session_token": None},
            queue="runs",
            routing_key="runs",
        )

        # test the response headers
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")

        # test significant response content
        self.assertEqual(
            response.data["exports"][0]["formats"][0]["slug"], request_data["provider_tasks"][0]["formats"][0]
        )
        self.assertEqual(
            response.data["exports"][0]["formats"][1]["slug"], request_data["provider_tasks"][0]["formats"][1]
        )
        self.assertEqual(response.data["name"], request_data["name"])
        self.assertEqual(response.data["description"], request_data["description"])

    def test_invalid_selection(
        self,
    ):
        url = reverse("api:jobs-list")
        formats = [export_format.slug for export_format in ExportFormat.objects.all()]
        request_data = {
            "name": "TestJob",
            "description": "Test description",
            "event": "Test Activation",
            "selection": {},
            "preset": self.job.preset.id,
            "provider_tasks": [{"provider": "osm-generic", "formats": formats}],
        }
        response = self.client.post(url, request_data, format="json")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.assertEqual("No Geometry", response.data["errors"][0]["title"])

    def test_empty_string_param(
        self,
    ):
        url = reverse("api:jobs-list")
        formats = [export_format.slug for export_format in ExportFormat.objects.all()]
        request_data = {
            "name": "TestJob",
            "description": "",  # empty
            "event": "Test Activation",
            "selection": bbox_to_geojson([5, 16, 5.1, 16.1]),
            "formats": formats,
        }
        response = self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.assertIsNotNone(response.data["errors"][0]["title"])

    def test_string_too_long_param(
        self,
    ):
        url = reverse("api:jobs-list")
        formats = [export_format.slug for export_format in ExportFormat.objects.all()]
        name = "x" * 300
        request_data = {
            "name": name,
            "description": "Test description",
            "event": "Test event",
            "selection": bbox_to_geojson([5, 16, 5.1, 16.1]),
            "provider_tasks": [{"provider": "osm-generic", "formats": formats}],
        }
        response = self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.assertEqual("Max Length", response.data["errors"][0]["title"])
        self.assertEqual(
            "name: Ensure this field has no more than 100 characters.", response.data["errors"][0]["detail"]
        )

    def test_missing_format_param(
        self,
    ):
        url = reverse("api:jobs-list")
        request_data = {
            "name": "TestJob",
            "description": "Test description",
            "event": "Test Activation",
            "selection": bbox_to_geojson([5, 16, 5.1, 16.1]),
            "provider_tasks": [{"provider": "osm-generic"}],  # 'formats': formats}]# missing
        }
        response = self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.assertIsNotNone(response.data["errors"][0]["title"])

    def test_invalid_format_param(
        self,
    ):
        url = reverse("api:jobs-list")
        request_data = {
            "name": "TestJob",
            "description": "Test description",
            "event": "Test Activation",
            "selection": bbox_to_geojson([5, 16, 5.1, 16.1]),
            "provider_tasks": [{"provider": "osm-generic", "formats": ""}],  # invalid
        }
        response = self.client.post(url, request_data, format="json")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.assertIsNotNone(response.data.get("errors")[0]["title"])

    def test_no_matching_format_slug(
        self,
    ):
        url = reverse("api:jobs-list")
        request_data = {
            "name": "TestJob",
            "description": "Test description",
            "event": "Test Activation",
            "selection": bbox_to_geojson([5, 16, 5.1, 16.1]),
            "provider_tasks": [{"provider": "osm-generic", "formats": ["broken-format-one", "broken-format-two"]}],
        }
        response = self.client.post(url, request_data, format="json")

        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.assertIsNotNone(response.data["errors"][0]["detail"])

    def test_extents_too_large(
        self,
    ):
        url = reverse("api:jobs-list")
        formats = [export_format.slug for export_format in ExportFormat.objects.all()]
        # job outside any region
        request_data = {
            "name": "TestJob",
            "description": "Test description",
            "event": "Test Activation",
            # Full world (WGS84) bounds are invalid in 3857 -- subtract 10 from lat as we simply need
            # a very large bounding box.
            "selection": bbox_to_geojson([-180, -80, 180, 80]),
            "provider_tasks": [{"provider": "osm-generic", "formats": formats}],
        }

        with self.settings(JOB_MAX_EXTENT=100000):
            response = self.client.post(
                url, data=json.dumps(request_data), content_type="application/json; version=1.0"
            )

        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.assertEqual("Invalid Extents", response.data["errors"][0]["title"])

    @patch("eventkit_cloud.api.views.get_estimate_cache_key")
    @patch("eventkit_cloud.api.views.cache")
    def test_extents_too_large_for_max_data_size(self, cache_mock, get_estimate_cache_key_mock):
        self.estimate_size = 210
        self.estimate_time = 20
        cache_mock.get.return_value = [self.estimate_size, self.estimate_time]

        cache_key = "1.22222222222222"
        get_estimate_cache_key_mock.return_value = cache_key
        bbox = (5, 16, 5.1, 16.1)
        srs = "4326"
        max_zoom = 17
        min_zoom = 0
        slug = "osm"
        excessive_data_size = 200

        add_max_data_size(self.provider, excessive_data_size)

        job_url = reverse("api:jobs-list")

        formats = [export_format.slug for export_format in ExportFormat.objects.all()]
        request_data = {
            "name": "TestJob",
            "description": "Test description",
            "event": "Test Activation",
            "selection": bbox_to_geojson(bbox),
            "provider_tasks": [
                {"provider": self.provider.slug, "formats": formats, "min_zoom": min_zoom, "max_zoom": max_zoom}
            ],
        }

        response = self.client.post(
            job_url, data=json.dumps(request_data), content_type="application/json; version=1.0"
        )
        cache_mock.get.assert_called_with(cache_key, (None, None))
        get_estimate_cache_key_mock.called_once_with(bbox, srs, min_zoom, max_zoom, slug)
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.assertEqual("Estimated size too large", response.data["errors"][0]["title"])

    def test_patch(self):
        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        self.assertEqual(expected, url)

        request_data = {"published": False}
        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertIsNotNone(response.data["published"])
        self.assertTrue(response.data["success"])

        request_data = {"featured": True}
        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertIsNotNone(response.data["featured"])
        self.assertTrue(response.data["success"])

        request_data = {"featured": True, "published": False, "visibility": VisibilityState.SHARED.value}
        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertIsNotNone(response.data["featured"])
        self.assertIsNotNone(response.data["published"])
        self.assertIsNotNone(response.data["visibility"])
        self.assertTrue(response.data["success"])


class TestBBoxSearch(APITestCase):
    """
    Test cases for testing bounding box searches.
    """

    fixtures = ("osm_provider.json", "datamodel_presets.json")

    def __init__(self, *args, **kwargs):
        super(TestBBoxSearch, self).__init__(*args, **kwargs)
        self.user = None
        self.client = None

    @patch("eventkit_cloud.api.views.pick_up_run_task")
    @patch("eventkit_cloud.api.views.create_run")
    def setUp(self, create_run_mock, pickup_mock):
        create_run_mock.return_value = "some_run_uid"

        url = reverse("api:jobs-list")

        self.group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo", is_superuser=True)

        # setup token authentication
        token = Token.objects.create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )
        # pull out the formats
        formats = [export_format.slug for export_format in ExportFormat.objects.all()]
        # create test jobs
        extents = [
            (-3.9, 16.1, 7.0, 27.6),
            (36.90, 13.54, 48.52, 20.24),
            (-71.79, -49.57, -67.14, -46.16),
            (-61.27, -6.49, -56.20, -2.25),
            (-11.61, 32.07, -6.42, 36.31),
            (-10.66, 5.81, -2.45, 11.83),
            (47.26, 34.58, 52.92, 39.15),
            (90.00, 11.28, 95.74, 17.02),
        ]
        for extent in extents:
            request_data = {
                "name": "TestJob",
                "description": "Test description",
                "event": "Test Activation",
                "selection": bbox_to_geojson(extent),
                "provider_tasks": [{"provider": "osm-generic", "formats": formats}],
            }
            response = self.client.post(url, request_data, format="json")
            expected_user_details = {"username": "demo", "is_superuser": True, "is_staff": False}
            pickup_mock.apply_async.assert_called_with(
                kwargs={"run_uid": "some_run_uid", "user_details": expected_user_details, "session_token": None},
                queue="runs",
                routing_key="runs",
            )
            self.assertEqual(status.HTTP_202_ACCEPTED, response.status_code, response.content)
        self.assertEqual(8, len(Job.objects.all()))
        LinkHeaderPagination.page_size = 2

    def test_bbox_search_success(
        self,
    ):
        url = reverse("api:jobs-list")
        extent = (-79.5, -16.16, 7.40, 52.44)
        param = "bbox={0},{1},{2},{3}".format(extent[0], extent[1], extent[2], extent[3])
        response = self.client.get("{0}?{1}".format(url, param))
        self.assertEqual(status.HTTP_206_PARTIAL_CONTENT, response.status_code)
        self.assertEqual(2, len(response.data))  # 8 jobs in total but response is paginated

    def test_list_jobs_no_bbox(
        self,
    ):
        url = reverse("api:jobs-list")
        response = self.client.get(url)
        self.assertEqual(status.HTTP_206_PARTIAL_CONTENT, response.status_code)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.assertEqual(response["Link"], '<http://testserver/api/jobs?page=2>; rel="next"')
        self.assertEqual(2, len(response.data))  # 8 jobs in total but response is paginated

    def test_bbox_search_missing_params(
        self,
    ):
        url = reverse("api:jobs-list")
        param = "bbox="  # missing params
        response = self.client.get("{0}?{1}".format(url, param))
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.assertEqual("Missing Bbox Parameter", response.data["errors"][0]["title"])

    def test_bbox_missing_coord(
        self,
    ):
        url = reverse("api:jobs-list")
        extent = (-79.5, -16.16, 7.40)  # one missing
        param = "bbox={0},{1},{2}".format(extent[0], extent[1], extent[2])
        response = self.client.get("{0}?{1}".format(url, param))
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.assertEqual("Missing Bbox Parameter", response.data["errors"][0]["title"])


class TestPagination(APITestCase):
    pass


class TestExportRunViewSet(APITestCase):
    """
    Test cases for ExportRunViewSet
    """

    fixtures = (
        "osm_provider.json",
        "datamodel_presets.json",
    )

    def __init__(self, *args, **kwargs):
        super(TestExportRunViewSet, self).__init__(*args, **kwargs)
        self.user = None
        self.client = None
        self.job = None
        self.job_uid = None
        self.export_run = None
        self.run_uid = None

    def setUp(
        self,
    ):
        self.group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo")
        self.attribute_class = AttributeClass.objects.create(name="test", slug="test", filter="username=demo")
        self.attribute_class.users.add(self.user)
        self.attribute_class.save()

        token = Token.objects.create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )
        extents = (-3.9, 16.1, 7.0, 27.6)
        bbox = Polygon.from_bbox(extents)
        the_geom = GEOSGeometry(bbox, srid=4326)
        self.job = Job.objects.create(name="TestJob", description="Test description", user=self.user, the_geom=the_geom)
        formats = ExportFormat.objects.all()
        self.provider = DataProvider.objects.first()
        self.provider.attribute_class = self.attribute_class
        self.provider.save()
        provider_task = DataProviderTask.objects.create(job=self.job, provider=self.provider)
        provider_task.formats.add(*formats)
        self.job.visibility = VisibilityState.PUBLIC.value
        self.job.save()
        self.job_uid = str(self.job.uid)
        self.export_run = ExportRun.objects.create(job=self.job, user=self.user)
        self.run_uid = str(self.export_run.uid)

    def test_patch(self):
        url = reverse("api:runs-detail", args=[self.export_run.uid])

        today = datetime.today()
        max_days = int(getattr(settings, "MAX_DATAPACK_EXPIRATION_DAYS", 30))
        ok_expiration = today + timedelta(max_days - 1)
        request_data = {"expiration": ok_expiration.isoformat()}
        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertIsNotNone(response.data["expiration"])
        self.assertTrue(response.data["success"])

        not_ok_expiration = ok_expiration - timedelta(1)
        request_data = {"expiration": not_ok_expiration.isoformat()}
        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertFalse(response.data["success"])

        not_ok_expiration = today + timedelta(max_days + 1)
        request_data = {"expiration": not_ok_expiration.isoformat()}
        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertFalse(response.data["success"])

        request_data = {"exploration": ok_expiration.isoformat()}
        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)

        run = ExportRun.objects.get(uid=self.export_run.uid)
        self.assertEqual(ok_expiration, run.expiration.replace(tzinfo=None))

    def test_delete_run(
        self,
    ):
        url = reverse("api:runs-detail", args=[self.export_run.uid])
        response = self.client.delete(url)
        # test the response headers
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(response["Content-Length"], "0")
        self.assertEqual(response["Content-Language"], "en")

    def test_retrieve_run(
        self,
    ):
        expected = "/api/runs/{0}".format(self.run_uid)

        url = reverse("api:runs-detail", args=[self.run_uid])
        self.assertEqual(expected, url)
        response = self.client.get(url)
        self.assertIsNotNone(response)
        result = response.data
        # make sure we get the correct uid back out
        self.assertEqual(self.run_uid, result[0].get("uid"))

    def test_retrieve_run_no_attribute_class(
        self,
    ):
        expected = "/api/runs/{0}".format(self.run_uid)
        self.attribute_class.users.remove(self.user)
        url = reverse("api:runs-detail", args=[self.run_uid])
        self.assertEqual(expected, url)
        response = self.client.get(url)
        self.assertIsNotNone(response)
        expected_result = {"provider_tasks": [], "provider_task_list_status": "EMPTY"}
        result = response.data
        # make sure we get the correct uid back out
        self.assertEqual(self.run_uid, result[0].get("uid"))
        self.assertEqual(expected_result["provider_tasks"], result[0]["provider_tasks"])
        self.assertEqual(expected_result["provider_task_list_status"], result[0]["provider_task_list_status"])

    @patch("eventkit_cloud.api.views.ExportRunViewSet.validate_licenses")
    def test_retrieve_run_invalid_license(self, mock_validate_licenses):
        expected = "/api/runs/{0}".format(self.run_uid)
        mock_validate_licenses.side_effect = (InvalidLicense("no license"),)
        url = reverse("api:runs-detail", args=[self.run_uid])
        self.assertEqual(expected, url)
        response = self.client.get(url)
        self.assertIsNotNone(response)
        result = response.data
        self.assertTrue("InvalidLicense" in result["errors"][0].get("detail"))
        self.assertEqual(response.status_code, 400)

    @patch("eventkit_cloud.api.views.get_invalid_licenses")
    def test_validate_licenses(self, mock_invalid_licenses):
        queryset = Mock()
        run = Mock(job=Mock(user=Mock(username="username")))
        queryset.all.return_value = [run]
        mock_invalid_licenses.return_value = ["license"]
        with self.assertRaises(InvalidLicense):
            ExportRunViewSet.validate_licenses(queryset)

        mock_invalid_licenses.return_value = []
        self.assertTrue(ExportRunViewSet.validate_licenses(queryset))

    def test_list_runs(
        self,
    ):
        expected = "/api/runs"
        url = reverse("api:runs-list")
        self.assertEqual(expected, url)
        query = "{0}?job_uid={1}".format(url, self.job.uid)
        response = self.client.get(query)
        self.assertIsNotNone(response)
        result = response.data
        # make sure we get the correct uid back out
        self.assertEqual(1, len(result))
        self.assertEqual(self.run_uid, result[0].get("uid"))

    @patch("eventkit_cloud.api.views.ExportRunViewSet.validate_licenses")
    def test_list_runs_invalid_license(self, mock_validate_licenses):
        from eventkit_cloud.tasks.task_factory import InvalidLicense

        expected = "/api/runs"
        url = reverse("api:runs-list")
        mock_validate_licenses.side_effect = (InvalidLicense("no license"),)
        self.assertEqual(expected, url)
        response = self.client.get(url)
        self.assertIsNotNone(response)
        result = response.data
        self.assertTrue("InvalidLicense" in result["errors"][0].get("detail"))
        self.assertEqual(response.status_code, 400)

    def test_filter_runs(
        self,
    ):
        expected = "/api/runs/filter"
        url = reverse("api:runs-filter")
        self.assertEqual(expected, url)
        query = "{0}".format(url)
        extents = (-4, 15, 8.0, 28)
        bbox = Polygon.from_bbox(extents)
        the_geom = GEOSGeometry(bbox, srid=4326)
        geojson = the_geom.geojson
        response = self.client.post(query, {"geojson": "{}".format(geojson)})
        self.assertIsNotNone(response)
        result = response.data
        # make sure we get a single run back
        self.assertEqual(1, len(result))
        self.assertEqual(self.run_uid, result[0].get("uid"))

        extents = (-3, 16, 7.0, 27)
        bbox = Polygon.from_bbox(extents)
        the_geom = GEOSGeometry(bbox, srid=4326)
        geojson = the_geom.geojson
        response = self.client.post(query, {"geojson": "{}".format(geojson)})
        self.assertIsNotNone(response)
        result = response.data
        # make sure 1 run is returned as it should be completely contained
        self.assertEqual(1, len(result))

        extents = (4, 17, 9.0, 28)
        bbox = Polygon.from_bbox(extents)
        the_geom = GEOSGeometry(bbox, srid=4326)
        geojson = the_geom.geojson
        response = self.client.post(query, {"geojson": "{}".format(geojson)})
        self.assertIsNotNone(response)
        result = response.data
        # make sure 1 run is returned as it should intersect
        self.assertEqual(1, len(result))

        extents = (-40, -5, -30, 5)
        bbox = Polygon.from_bbox(extents)
        the_geom = GEOSGeometry(bbox, srid=4326)
        geojson = the_geom.geojson
        response = self.client.post(query, {"geojson": "{}".format(geojson)})
        self.assertIsNotNone(response)
        result = response.data
        # make sure no runs are returned since it should not intersect
        self.assertEqual(0, len(result))

        name = "TestJob"
        query = "{0}?search_term={1}".format(url, name)
        response = self.client.get(query)
        self.assertIsNotNone(response)
        result = response.data
        # make sure we get a single run back
        self.assertEqual(1, len(result))
        self.assertEqual(name, result[0].get("job").get("name"))

        name = "NotFound"
        query = "{0}?search_term={1}".format(url, name)
        response = self.client.get(query)
        self.assertIsNotNone(response)
        result = response.data
        # make sure no runs are returned as they should have been filtered out
        self.assertEqual(0, len(result))

    @patch("eventkit_cloud.api.views.rerun_data_provider_records")
    @patch("eventkit_cloud.api.views.check_job_permissions")
    def test_rerun_providers(self, mock_check_job_permissions, mock_rerun_records):
        run = ExportRun.objects.create(job=self.job, user=self.user)
        data_provider_task_record = DataProviderTaskRecord.objects.create(
            run=run, name="Shapefile Export", provider=self.provider, status=TaskState.PENDING.value
        )
        run.data_provider_task_records.add(data_provider_task_record)

        expected_user_details = {"username": "demo", "is_superuser": False, "is_staff": False}
        url = f"/api/runs/{run.uid}/rerun_providers"
        expected_slugs = ["osm-generic"]
        request_data = {"data_provider_slugs": expected_slugs}

        response = self.client.post(url, request_data, format="json")
        self.assertEqual(status.HTTP_202_ACCEPTED, response.status_code)
        self.assertEqual("PENDING", response.data["zipfile"]["status"])

        mock_check_job_permissions.assert_called_once_with(run.job)
        mock_rerun_records.apply_async.assert_called_once_with(
            args=(run.uid, self.user.id, expected_user_details, expected_slugs), queue="runs", routing_key="runs"
        )


class TestRunZipFileViewSet(APITestCase):
    """
    Test cases for RunZipFileViewSet
    """

    fixtures = ("osm_provider.json",)

    list_url = reverse("api:zipfiles-list")

    def setUp(self):
        self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo")
        self.token = Token.objects.create(user=self.user)

        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + self.token.key,
            HTTP_ACCEPT="application/json, text/plain; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

        bbox = Polygon.from_bbox((-7.96, 22.6, -8.14, 27.12))
        the_geom = GEOSGeometry(bbox, srid=4326)
        self.job = Job.objects.create(
            name="TestJob",
            description="Test description",
            user=self.user,
            the_geom=the_geom,
            visibility=VisibilityState.PUBLIC.value,
        )
        self.run = ExportRun.objects.create(job=self.job, user=self.user)

        self.provider = DataProvider.objects.first()
        self.celery_uid = str(uuid.uuid4())
        self.data_provider_task_record = DataProviderTaskRecord.objects.create(
            run=self.run, name="Shapefile Export", provider=self.provider, status=TaskState.PENDING.value
        )

        filename = "test.zip"
        self.downloadable_file = FileProducingTaskResult.objects.create(filename=filename, size=10)
        self.task = ExportTaskRecord.objects.create(
            export_provider_task=self.data_provider_task_record,
            name="Shapefile Export",
            celery_uid=self.celery_uid,
            status="SUCCESS",
            result=self.downloadable_file,
        )
        self.run_zip_file = RunZipFile.objects.create(run=self.run, downloadable_file=self.downloadable_file)

        self.data_provider_task_records = [self.data_provider_task_record]
        self.run_zip_file.data_provider_task_records.set(self.data_provider_task_records)

    def test_zipfiles_list_authenticated(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_zipfiles_list_unauthenticated(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_zipfiles_detail_retrieve(self):
        url = reverse("api:zipfiles-detail", args=[self.run_zip_file.uid])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(str(response.data.get("uid")), str(self.run_zip_file.uid))
        self.assertEqual(str(response.data.get("run")), str(self.run_zip_file.run))

        dptr_uids = [dptr.uid for dptr in self.run_zip_file.data_provider_task_records.all()]
        self.assertEqual(str(response.data.get("data_provider_task_records")), str(dptr_uids))

        self.assertEqual(response.data.get("downloadable_file"), self.run_zip_file.downloadable_file.id)

        expected_url = f"http://testserver/download?uid={self.run_zip_file.downloadable_file.uid}"
        self.assertEqual(response.data.get("url"), expected_url)


class TestDataProviderTaskRecordViewSet(APITestCase):
    """
    Test cases for ExportTaskViewSet
    """

    fixtures = (
        "osm_provider.json",
        "datamodel_presets.json",
    )

    def __init__(self, *args, **kwargs):
        super(TestDataProviderTaskRecordViewSet, self).__init__(*args, **kwargs)
        self.user = None
        self.path = None
        self.job = None
        self.celery_uid = None
        self.client = None
        self.export_run = None
        self.data_provider_task_record = None
        self.task = None
        self.task_uid = None

    def setUp(
        self,
    ):
        self.path = os.path.dirname(os.path.realpath(__file__))
        self.group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo")
        self.attribute_class = AttributeClass.objects.create(name="test", slug="test", filter="username=demo")
        self.attribute_class.users.add(self.user)
        self.attribute_class.save()
        bbox = Polygon.from_bbox((-7.96, 22.6, -8.14, 27.12))
        the_geom = GEOSGeometry(bbox, srid=4326)
        self.job = Job.objects.create(
            name="TestJob",
            description="Test description",
            user=self.user,
            the_geom=the_geom,
            visibility=VisibilityState.PUBLIC.value,
        )

        formats = ExportFormat.objects.all()
        self.provider = DataProvider.objects.first()
        self.provider.attribute_class = self.attribute_class
        self.provider.save()
        provider_task = DataProviderTask.objects.create(job=self.job, provider=self.provider)
        provider_task.formats.add(*formats)
        self.job.save()

        # setup token authentication
        token = Token.objects.create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )
        self.export_run = ExportRun.objects.create(job=self.job, user=self.user)
        self.celery_uid = str(uuid.uuid4())
        self.data_provider_task_record = DataProviderTaskRecord.objects.create(
            run=self.export_run, name="Shapefile Export", provider=self.provider, status=TaskState.PENDING.value
        )
        self.task = ExportTaskRecord.objects.create(
            export_provider_task=self.data_provider_task_record,
            name="Shapefile Export",
            celery_uid=self.celery_uid,
            status="SUCCESS",
        )
        self.task_uid = str(self.task.uid)

    def test_retrieve(
        self,
    ):
        url = reverse("api:provider_tasks-detail", args=[self.data_provider_task_record.uid])
        response = self.client.get(url)
        self.assertIsNotNone(response)
        self.assertEqual(200, response.status_code)
        # make sure we get the correct uid back out
        expected_response = {"hidden": False, "display": False}
        self.assertEqual(str(response.data.get("uid")), str(self.data_provider_task_record.uid))

        self.assertEqual(response.data["hidden"], expected_response["hidden"])
        self.assertEqual(response.data["display"], expected_response["display"])
        self.attribute_class.users.remove(self.user)
        response = self.client.get(url)
        expected_response = {"hidden": True, "display": False}
        self.assertEqual(str(response.data.get("uid")), str(self.data_provider_task_record.uid))
        self.assertEqual(response.data["hidden"], expected_response["hidden"])
        self.assertEqual(response.data["display"], expected_response["display"])

    def test_list(
        self,
    ):
        url = reverse("api:provider_tasks-list")
        response = self.client.get(url)
        self.assertIsNotNone(response)
        self.assertEqual(200, response.status_code)
        # make sure we get the correct uid back out
        expected_response = {"hidden": False, "display": False}
        self.assertEqual(str(response.data[0].get("uid")), str(self.data_provider_task_record.uid))
        self.assertEqual(response.data[0]["hidden"], expected_response["hidden"])
        self.assertEqual(response.data[0]["display"], expected_response["display"])
        self.attribute_class.users.remove(self.user)
        response = self.client.get(url)
        expected_response = {"hidden": True, "display": False}
        self.assertEqual(str(response.data[0].get("uid")), str(self.data_provider_task_record.uid))
        self.assertEqual(response.data[0]["hidden"], expected_response["hidden"])
        self.assertEqual(response.data[0]["display"], expected_response["display"])

    def test_patch_cancel_task(
        self,
    ):
        expected = "/api/provider_tasks/{0}".format(self.data_provider_task_record.uid)
        url = reverse("api:provider_tasks-list") + "/%s" % (self.data_provider_task_record.uid,)
        self.assertEqual(expected, url)
        response = self.client.patch(url)
        # test significant content
        self.assertEqual(response.data, {"success": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        pt = DataProviderTaskRecord.objects.get(uid=self.data_provider_task_record.uid)
        et = pt.tasks.last()

        self.assertEqual(pt.status, TaskState.CANCELED.value)
        self.assertEqual(et.status, TaskState.SUCCESS.value)

    def test_patch_cancel_task_no_permissions(
        self,
    ):
        user = User.objects.create_user(username="demo2", email="demo2@demo.com", password="demo")
        self.attribute_class.users.add(user)
        token = Token.objects.create(user=user)
        # reset the client credentials to the new user
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )
        expected = "/api/provider_tasks/{0}".format(self.data_provider_task_record.uid)
        url = reverse("api:provider_tasks-list") + "/%s" % (self.data_provider_task_record.uid,)
        self.assertEqual(expected, url)
        response = self.client.patch(url)
        # test the response headers
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data, {"success": False})
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")
        self.attribute_class.users.remove(user)
        response = self.client.patch(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data, {"success": False})

    def test_export_provider_task_get(self):
        url = reverse("api:provider_tasks-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class TestExportTaskViewSet(APITestCase):
    """
    Test cases for ExportTaskViewSet
    """

    fixtures = (
        "osm_provider.json",
        "datamodel_presets.json",
    )

    def __init__(self, *args, **kwargs):
        super(TestExportTaskViewSet, self).__init__(*args, **kwargs)
        self.user = None
        self.path = None
        self.job = None
        self.celery_uid = None
        self.client = None
        self.export_run = None
        self.export_provider_task = None
        self.task = None
        self.task_uid = None

    def setUp(
        self,
    ):
        self.path = os.path.dirname(os.path.realpath(__file__))
        self.group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo")
        self.attribute_class = AttributeClass.objects.create(name="test", slug="test", filter="username=demo")
        self.attribute_class.users.add(self.user)
        self.attribute_class.save()
        bbox = Polygon.from_bbox((-7.96, 22.6, -8.14, 27.12))
        the_geom = GEOSGeometry(bbox, srid=4326)
        self.job = Job.objects.create(
            name="TestJob",
            description="Test description",
            user=self.user,
            the_geom=the_geom,
            visibility=VisibilityState.PUBLIC.value,
        )

        formats = ExportFormat.objects.all()
        self.provider = DataProvider.objects.first()
        provider_task = DataProviderTask.objects.create(job=self.job, provider=self.provider)
        provider_task.formats.add(*formats)
        self.job.save()

        # setup token authentication
        token = Token.objects.create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )
        self.export_run = ExportRun.objects.create(job=self.job, user=self.user)
        self.celery_uid = str(uuid.uuid4())
        self.export_provider_task = DataProviderTaskRecord.objects.create(
            run=self.export_run, name="Shapefile Export", provider=self.provider, status=TaskState.PENDING.value
        )
        self.task = ExportTaskRecord.objects.create(
            export_provider_task=self.export_provider_task,
            name="Shapefile Export",
            celery_uid=self.celery_uid,
            status="SUCCESS",
        )
        self.task_uid = str(self.task.uid)

    def test_retrieve(
        self,
    ):
        expected = "/api/tasks/{0}".format(self.task_uid)
        url = reverse("api:tasks-detail", args=[self.task_uid])
        self.assertEqual(expected, url)
        response = self.client.get(url)
        self.assertIsNotNone(response)
        self.assertEqual(200, response.status_code)
        result = json.dumps(response.data)
        data = json.loads(result)
        # make sure we get the correct uid back out
        self.assertEqual(self.task_uid, data[0].get("uid"))
        response = self.client.get(url)

    def test_list(
        self,
    ):
        expected = "/api/tasks"
        url = reverse("api:tasks-list")
        self.assertEqual(expected, url)
        response = self.client.get(url)
        self.assertIsNotNone(response)
        self.assertEqual(200, response.status_code)
        result = json.dumps(response.data)
        data = json.loads(result)
        # should only be one task in the list
        self.assertEqual(1, len(data))
        # make sure we get the correct uid back out
        self.assertEqual(self.task_uid, data[0].get("uid"))


class TestStaticFunctions(APITestCase):
    def test_get_models(self):
        ExportFormat.objects.create(name="Test1", slug="Test1")
        ExportFormat.objects.create(name="Test2", slug="Test2")
        sample_models = ["Test1", "Test2"]
        models = get_models(sample_models, ExportFormat, "name")
        assert len(models) == 2

    def test_get_provider_tasks(self):
        # Arbitrary "Formats"
        format_test1 = ExportFormat.objects.create(name="Test1", slug="Test1")
        format_test2 = ExportFormat.objects.create(name="Test2", slug="Test2")
        format_test3 = ExportFormat.objects.create(name="Test3", slug="Test3")

        # Formats we want to process
        requested_types = (format_test1, format_test2)

        # An arbitrary provider type...
        provider_type = DataProviderType.objects.create(type_name="test")
        # ... and the formats it actually supports.
        supported_formats = [format_test2, format_test3]
        provider_type.supported_formats.add(*supported_formats)
        provider_type.save()

        # Assign the type to an arbitrary provider.
        export_provider = DataProvider.objects.create(name="provider1", export_provider_type=provider_type)
        # Get a DataProviderTask object to ensure that it is only trying to process
        # what it actually supports (1).
        provider_task = get_provider_task(export_provider, requested_types)

        assert len(provider_task.formats.all()) == 2


class TestLicenseViewSet(APITestCase):
    def setUp(
        self,
    ):
        group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo")
        self.licenses = [License.objects.create(slug="test1", name="name1", text="text1")]
        self.licenses += [License.objects.create(slug="test0", name="name0", text="text0")]
        token = Token.objects.create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json, text/plain; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

    def test_get_licenses_list(self):
        expected_url = "/api/licenses"
        expected_data = [
            {"slug": "test0", "name": "name0", "text": "text0"},
            {"slug": "test1", "name": "name1", "text": "text1"},
        ]
        url = reverse("api:licenses-list")
        self.assertEqual(expected_url, url)
        response = self.client.get(url)
        self.assertIsNotNone(response)
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(expected_data, data)

    def test_get_licenses_detail(self):
        expected_url = "/api/licenses/test1"
        expected_data = {"slug": "test1", "name": "name1", "text": "text1"}
        url = reverse("api:licenses-detail", args=["test1"])
        self.assertEqual(expected_url, url)
        response = self.client.get(url)
        self.assertIsNotNone(response)
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(expected_data, data)

    def test_get_licenses_download(self):
        expected_url = "/api/licenses/test1/download"
        url = reverse("api:licenses-download", args=["test1"])
        self.assertEqual(expected_url, url)
        response = self.client.get(url)
        self.assertIsNotNone(response)
        self.assertEqual(200, response.status_code)
        self.assertEqual(self.licenses[0].text, response.content.decode())

        expected_bad_url = "/api/licenses/test22/download"
        bad_url = reverse("api:licenses-download", args=["test22"])
        self.assertEqual(expected_bad_url, bad_url)
        bad_response = self.client.get(bad_url)
        self.assertIsNotNone(bad_response)
        self.assertEqual(404, bad_response.status_code)
        self.assertEqual("Not Found", bad_response.data["errors"][0]["title"])


class TestUserDataViewSet(APITestCase):
    fixtures = ("osm_provider.json",)

    def setUp(
        self,
    ):
        self.path = os.path.dirname(os.path.realpath(__file__))
        self.group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo")
        self.licenses = [License.objects.create(slug="test1", name="Test1", text="text")]
        self.licenses += [License.objects.create(slug="test2", name="Test2", text="text")]
        token = Token.objects.create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

        ds = DataSource(os.path.dirname(os.path.realpath(__file__)) + "/../../jobs/migrations/africa.geojson")
        layer = ds[0]
        geom = layer.get_geoms(geos=True)[0]
        the_geom = GEOSGeometry(geom.wkt, srid=4326)
        the_geog = GEOSGeometry(geom.wkt)
        the_geom_webmercator = the_geom.transform(ct=3857, clone=True)
        region = Region.objects.create(
            name="Africa",
            description="African export region",
            the_geom=the_geom,
            the_geog=the_geog,
            the_geom_webmercator=the_geom_webmercator,
        )

        self.provider = DataProvider.objects.first()

        policies_example = json.loads(get_example_from_file("examples/policies_example.json"))
        justification_options_example = json.loads(get_example_from_file("examples/justification_options_example.json"))

        self.regional_policy = RegionalPolicy.objects.create(
            name="Test Policy",
            region=region,
            policies=policies_example,
            justification_options=justification_options_example,
            policy_title_text="Policy Title",
            policy_cancel_button_text="Cancel Button",
        )
        self.regional_policy.providers.set([self.provider])
        self.regional_policy.save()

    def test_get_userdata_list(self):
        expected = "/api/users"
        url = reverse("api:users-list")
        self.assertEqual(expected, url)
        response = self.client.get(url)
        self.assertIsNotNone(response)
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(self.user.username, data[0].get("user").get("username"))
        self.assertIsNotNone(data[0].get("accepted_licenses").get(self.licenses[0].slug))

    def test_get_userdata(self):
        expected = "/api/users/{0}".format(self.user)
        url = reverse("api:users-detail", args=[self.user])
        self.assertEqual(expected, url)
        response = self.client.get(url)
        self.assertIsNotNone(response)
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(self.user.username, data.get("user").get("username"))
        self.assertIsNotNone(data.get("accepted_licenses").get(self.licenses[0].slug))

    def test_set_licenses(self):
        url = reverse("api:users-detail", args=[self.user])
        response = self.client.get(url)
        data = response.json()
        # check both licenses are NOT accepted.
        self.assertEqual(data.get("accepted_licenses").get(self.licenses[0].slug), False)
        self.assertEqual(data.get("accepted_licenses").get(self.licenses[1].slug), False)
        # update single license.
        request_data = data
        request_data["accepted_licenses"][self.licenses[0].slug] = True
        patch_response = self.client.patch(
            url, data=json.dumps(request_data), content_type="application/json; version=1.0"
        )
        response = self.client.get(url)
        data = response.json()
        # check that the response body matches a new request
        self.assertEqual(patch_response.data, response.data)
        # check single licenses is accepted.
        self.assertEqual(data.get("accepted_licenses").get(self.licenses[0].slug), True)
        self.assertEqual(data.get("accepted_licenses").get(self.licenses[1].slug), False)
        request_data = data
        request_data["accepted_licenses"][self.licenses[1].slug] = True
        patch_response = self.client.patch(
            url, data=json.dumps(request_data), content_type="application/json; version=1.0"
        )
        data = patch_response.json()
        self.assertEqual(data.get("accepted_licenses").get(self.licenses[0].slug), True)
        self.assertEqual(data.get("accepted_licenses").get(self.licenses[1].slug), True)
        request_data = data
        request_data["accepted_licenses"][self.licenses[0].slug] = False
        patch_response = self.client.patch(
            url, data=json.dumps(request_data), content_type="application/json; version=1.0"
        )
        data = patch_response.json()
        self.assertEqual(data.get("accepted_licenses").get(self.licenses[0].slug), False)
        self.assertEqual(data.get("accepted_licenses").get(self.licenses[1].slug), True)

    def test_get_policies(self):
        self.user.last_login = timezone.now()
        self.user.save()
        url = reverse("api:users-detail", args=[self.user])
        response = self.client.get(url)
        data = response.json()
        # check both licenses are NOT accepted.
        self.assertEqual(data.get("accepted_policies"), {str(RegionalPolicy.objects.first().uid): False})

        request_data = {
            "justification_id": 1,
            "justification_suboption_value": "Option 1",
            "regional_policy_uid": str(self.regional_policy.uid),
        }
        url = reverse("api:regional_justifications-list")
        response = self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")

        url = reverse("api:users-detail", args=[self.user])
        response = self.client.get(url)
        data = response.json()
        self.assertEqual(data.get("accepted_policies"), {str(RegionalPolicy.objects.first().uid): True})


class TestGroupDataViewSet(APITestCase):
    def setUp(
        self,
    ):
        self.path = os.path.dirname(os.path.realpath(__file__))
        self.user1 = User.objects.create_user(username="user_1", email="groupuser@demo.com", password="demo")
        self.user2 = User.objects.create_user(username="user_2", email="groupuser@demo.com", password="demo")

        self.token = Token.objects.create(user=self.user1)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + self.token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

        self.testName = "Omaha 319"
        group, created = Group.objects.get_or_create(name=self.testName)
        self.groupid = group.id
        GroupPermission.objects.create(group=group, user=self.user1, permission=GroupPermissionLevel.ADMIN.value)

    def test_insert_group(self):
        expected = "/api/groups"
        url = reverse("api:groups-list")
        self.assertEqual(expected, url)
        payload = {"name": "Any group"}
        response = self.client.post(url, data=json.dumps(payload), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

    def test_duplicate_group(self):
        url = reverse("api:groups-list")
        payload = {"name": "oMaHa 319"}
        response = self.client.post(url, data=json.dumps(payload), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)

    def test_get_list(self):
        url = reverse("api:groups-list")
        response = self.client.get(url)
        self.assertIsNotNone(response)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        data = response.json()
        self.assertEqual(len(data), 2)

    def test_get_group(self):
        url = reverse("api:groups-detail", args=[self.groupid])
        response = self.client.get(url, content_type="application/json; version=1.0")
        data = response.json()
        self.groupid = data["id"]
        self.assertEqual(data["name"], self.testName)
        self.assertEqual(len(data["members"]), 0)
        self.assertEqual(len(data["administrators"]), 1)

    def test_set_membership(self):
        url = reverse("api:groups-detail", args=[self.groupid])
        response = self.client.get(url, content_type="application/json; version=1.0")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        groupdata = response.json()

        # add a user to group members and to group administrators

        groupdata["members"].append("user_1")
        groupdata["administrators"].append("user_1")
        groupdata["members"].append("user_2")
        groupdata["administrators"].append("user_2")
        response = self.client.patch(url, data=json.dumps(groupdata), content_type="application/json; version=1.0")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.get(url, content_type="application/json; version=1.0")
        groupdata = response.json()
        self.assertEqual(len(groupdata["members"]), 2)
        self.assertEqual(len(groupdata["administrators"]), 2)

        # remove user_2 from members

        groupdata["members"] = ["user_1"]
        groupdata["administrators"] = ["user_1"]
        response = self.client.patch(url, data=json.dumps(groupdata), content_type="application/json; version=1.0")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.get(url, content_type="application/json; version=1.0")
        groupdata = response.json()
        self.assertEqual(len(groupdata["members"]), 1)
        self.assertEqual(groupdata["members"][0], "user_1")

        # check for a 403 if we remove all administrators

        groupdata["administrators"] = []
        response = self.client.patch(url, data=json.dumps(groupdata), content_type="application/json; version=1.0")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_leave_group(self):
        # ensure the group is created
        url = reverse("api:groups-detail", args=[self.groupid])
        response = self.client.get(url, content_type="application/json; version=1.0")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # check add user_2 as member and only one admin
        group_data = response.json()
        group_data["members"] = ["user_1", "user_2"]
        group_data["administrators"] = ["user_2"]
        response = self.client.patch(url, data=json.dumps(group_data), content_type="application/json; version=1.0")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.get(url, content_type="application/json; version=1.0")
        group_data = response.json()
        self.assertEqual(len(group_data["members"]), 2)
        self.assertEqual(len(group_data["administrators"]), 1)
        self.assertEqual(group_data["administrators"][0], "user_2")

        # empty patch request should remove user_1 from members
        response = self.client.patch(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # verify the results
        response = self.client.get(url, content_type="application/json; verison=1.0")
        group_data = response.json()
        self.assertEqual(len(group_data["members"]), 1)
        self.assertEqual(group_data["members"][0], "user_2")


class TestUserJobActivityViewSet(APITestCase):
    fixtures = ("osm_provider.json",)

    def __init__(self, *args, **kwargs):
        super(TestUserJobActivityViewSet, self).__init__(*args, **kwargs)
        self.group = None
        self.user = None
        self.viewed_jobs = []

    def setUp(self):
        self.group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        with patch("eventkit_cloud.jobs.signals.Group") as mock_group:
            mock_group.objects.get.return_value = self.group
            self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo")
        token = Token.objects.create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

        for i in range(3):
            job = self.create_job("ViewedJob%s" % str(i))
            self.viewed_jobs.append(job)
            UserJobActivity.objects.create(user=self.user, job=job, type=UserJobActivity.VIEWED)

    def test_get_viewed(self):
        expected = "/api/user/activity/jobs"
        url = reverse("api:user_job_activity-list")
        self.assertEqual(expected, url)
        response = self.client.get(url + "?activity=viewed&page_size=10")
        self.assertIsNotNone(response)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        data = response.json()
        self.assertEqual(len(data), len(self.viewed_jobs))

    def test_get_viewed_pagination(self):
        for i in range(len(self.viewed_jobs), 15):
            job = self.create_job("ViewedJob%s" % str(i))
            self.viewed_jobs.append(job)
            UserJobActivity.objects.create(user=self.user, job=job, type=UserJobActivity.VIEWED)

        url = reverse("api:user_job_activity-list")
        page_size = 10
        response = self.client.get(url + "?activity=viewed&page_size=%s" % page_size)
        self.assertIsNotNone(response)
        self.assertEqual(status.HTTP_206_PARTIAL_CONTENT, response.status_code)
        data = response.json()
        self.assertEqual(len(data), page_size)

    def test_create_viewed(self):
        # Get our current number of viewed jobs to compare against.
        url = reverse("api:user_job_activity-list")
        response = self.client.get(url + "?activity=viewed&page_size=10")
        prev_viewed_jobs_count = len(response.json())

        # Post a new job view.
        job = self.create_job("UnviewedJob")
        response = self.client.post(
            url + "?activity=viewed",
            data=json.dumps({"job_uid": str(job.uid)}),
            content_type="application/json; version=1.0",
        )
        self.assertIsNotNone(response)
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        # Get our new number of viewed jobs and compare.
        response = self.client.get(url + "?activity=viewed&page_size=10")
        viewed_jobs = response.json()
        self.assertEqual(len(viewed_jobs), prev_viewed_jobs_count + 1)

        # Make sure the first returned viewed job matches what we just viewed.
        self.assertEqual(viewed_jobs[0]["last_export_run"]["job"]["uid"], str(job.uid))
        self.assertEqual(viewed_jobs[0]["type"], UserJobActivity.VIEWED)

    def test_create_viewed_consecutive(self):
        # Add one viewed job first.
        url = reverse("api:user_job_activity-list")
        job_a = self.create_job("UnviewedJobA")
        self.client.post(
            url + "?activity=viewed",
            data=json.dumps({"job_uid": str(job_a.uid)}),
            content_type="application/json; version=1.0",
        )

        # Post a new job view twice. It should be ignored the second time.
        job_b = self.create_job("UnviewedJobB")
        response = self.client.post(
            url + "?activity=viewed",
            data=json.dumps({"job_uid": str(job_b.uid)}),
            content_type="application/json; version=1.0",
        )
        self.assertEqual(response.json().get("ignored"), None)
        response = self.client.post(
            url + "?activity=viewed",
            data=json.dumps({"job_uid": str(job_b.uid)}),
            content_type="application/json; version=1.0",
        )
        self.assertEqual(response.json().get("ignored"), True)

        # Make sure we don't see the same job twice in our viewed jobs.
        response = self.client.get(url + "?activity=viewed")
        viewed_jobs = response.json()
        self.assertNotEqual(viewed_jobs[0], viewed_jobs[1])

    def test_create_viewed_existing(self):
        # View job A.
        url = reverse("api:user_job_activity-list")
        job_a = self.create_job("UnviewedJobA")
        self.client.post(
            url + "?activity=viewed",
            data=json.dumps({"job_uid": str(job_a.uid)}),
            content_type="application/json; version=1.0",
        )

        # View job B.
        job_b = self.create_job("UnviewedJobB")
        self.client.post(
            url + "?activity=viewed",
            data=json.dumps({"job_uid": str(job_b.uid)}),
            content_type="application/json; version=1.0",
        )

        # View Job A.
        self.client.post(
            url + "?activity=viewed",
            data=json.dumps({"job_uid": str(job_a.uid)}),
            content_type="application/json; version=1.0",
        )

        # Make sure that job A only shows up once in our viewed jobs.
        response = self.client.get(url + "?activity=viewed")
        viewed_jobs = response.json()
        job_a_count = 0
        for viewed_job in viewed_jobs:
            if viewed_job["last_export_run"]["job"]["uid"] == str(job_a.uid):
                job_a_count += 1

        self.assertEqual(job_a_count, 1)
        self.assertEqual(viewed_jobs[-1]["last_export_run"]["job"]["uid"], str(job_a.uid))

    def create_job(self, name):
        extents = (-3.9, 16.1, 7.0, 27.6)
        bbox = Polygon.from_bbox(extents)
        the_geom = GEOSGeometry(bbox, srid=4326)
        job = Job.objects.create(name=name, description="Test description", user=self.user, the_geom=the_geom)
        formats = ExportFormat.objects.all()
        provider = DataProvider.objects.first()
        provider_task = DataProviderTask.objects.create(job=job, provider=provider)
        provider_task.formats.add(*formats)
        run = ExportRun.objects.create(
            job=job, user=self.user, status="COMPLETED", expiration=(timezone.now() + timezone.timedelta(days=14))
        )
        job.last_export_run = run
        job.save()
        return job


class TestDataProviderRequestViewSet(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo")
        self.provider_request = DataProviderRequest(
            name="Test Data Provider Request",
            url="http://www.test.com",
            service_description="Test Service Description",
            layer_names="[Test1, Test2, Test3]",
            comment="Test Comment",
            user=self.user,
        )

        token = Token.objects.create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )
        self.provider_request.save()

    def test_list(self):
        expected = "/api/providers/requests"
        url = reverse("api:provider_requests-list")
        self.assertEqual(expected, url)

    def test_create_provider_request_success(self):

        request_data = {
            "name": "Test Name",
            "url": "http://www.test.com",
            "service_description": "Test Description",
            "layer_names": "[Test1, Test2, Test3]",
        }

        url = reverse("api:provider_requests-list")
        response = self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        response = response.json()

        provider_request = DataProviderRequest.objects.last()
        self.assertEqual(provider_request.name, request_data["name"])
        self.assertEqual(provider_request.url, request_data["url"])
        self.assertEqual(provider_request.service_description, request_data["service_description"])
        self.assertEqual(provider_request.layer_names, request_data["layer_names"])
        self.assertEqual(provider_request.status, "pending")
        self.assertEqual(provider_request.user, self.user)

    def test_get_provider_request_detail(self):
        expected = f"/api/providers/requests/{self.provider_request.uid}"
        url = reverse("api:provider_requests-detail", args=[self.provider_request.uid])
        self.assertEqual(expected, url)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")

        self.assertEqual(response.data["uid"], str(self.provider_request.uid))

    def test_get_provider_request_detail_no_permissions(self):
        user = User.objects.create_user(username="demo2", email="demo2@demo.com", password="demo")
        token = Token.objects.create(user=user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

        expected = f"/api/providers/requests/{self.provider_request.uid}"
        url = reverse("api:provider_requests-detail", args=[self.provider_request.uid])
        self.assertEqual(expected, url)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")

        self.assertIsNotNone(response.data["errors"][0]["detail"])

    def test_delete_job(
        self,
    ):
        url = reverse("api:provider_requests-detail", args=[self.provider_request.uid])
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(response["Content-Length"], "0")
        self.assertEqual(response["Content-Language"], "en")


class TestSizeIncreaseRequestViewSet(APITestCase):
    fixtures = ("osm_provider.json",)

    def setUp(self):
        bbox = Polygon.from_bbox((-7.96, 22.6, -8.14, 27.12))
        the_geom = GEOSGeometry(bbox, srid=4326)
        provider = DataProvider.objects.get(slug="osm-generic")
        self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo")
        self.provider = DataProvider.objects.first()
        self.size_request = SizeIncreaseRequest(
            provider=provider, the_geom=the_geom, requested_aoi_size=5000, requested_data_size=1000, user=self.user
        )

        token = Token.objects.create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )
        self.size_request.save()

    def test_list(self):
        expected = "/api/providers/requests/size"
        url = reverse("api:size_requests-list")
        self.assertEqual(expected, url)

    def test_create_size_request_success(self):

        request_data = {
            "provider": self.provider.id,
            "selection": bbox_to_geojson([5, 16, 5.1, 16.1]),
            "requested_aoi_size": 5000,
            "requested_data_size": 1000,
        }

        url = reverse("api:size_requests-list")
        response = self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        response = response.json()

        size_request = SizeIncreaseRequest.objects.last()
        self.assertEqual(size_request.provider.id, request_data["provider"])
        self.assertEqual(size_request.requested_aoi_size, request_data["requested_aoi_size"])
        self.assertEqual(size_request.requested_data_size, request_data["requested_data_size"])
        self.assertEqual(size_request.status, "pending")
        self.assertEqual(size_request.user, self.user)

    def test_get_size_request_detail(self):
        expected = f"/api/providers/requests/size/{self.size_request.uid}"
        url = reverse("api:size_requests-detail", args=[self.size_request.uid])
        self.assertEqual(expected, url)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")

        self.assertEqual(response.data["uid"], str(self.size_request.uid))

    def test_get_size_request_detail_no_permissions(self):
        user = User.objects.create_user(username="demo2", email="demo2@demo.com", password="demo")
        token = Token.objects.create(user=user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

        expected = f"/api/providers/requests/size/{self.size_request.uid}"
        url = reverse("api:size_requests-detail", args=[self.size_request.uid])
        self.assertEqual(expected, url)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")

        self.assertIsNotNone(response.data["errors"][0]["detail"])

    def test_delete_job(
        self,
    ):
        url = reverse("api:size_requests-detail", args=[self.size_request.uid])
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(response["Content-Length"], "0")
        self.assertEqual(response["Content-Language"], "en")


class TestRegionalJustification(APITestCase):
    fixtures = ("osm_provider.json",)

    def setUp(self):
        self.user = User.objects.create_user(username="demo", email="demo@demo.com", password="demo")
        token = Token.objects.create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

        ds = DataSource(os.path.dirname(os.path.realpath(__file__)) + "/../../jobs/migrations/africa.geojson")
        layer = ds[0]
        geom = layer.get_geoms(geos=True)[0]
        the_geom = GEOSGeometry(geom.wkt, srid=4326)
        the_geog = GEOSGeometry(geom.wkt)
        the_geom_webmercator = the_geom.transform(ct=3857, clone=True)
        region = Region.objects.create(
            name="Africa",
            description="African export region",
            the_geom=the_geom,
            the_geog=the_geog,
            the_geom_webmercator=the_geom_webmercator,
        )

        self.provider = DataProvider.objects.first()

        policies_example = json.loads(get_example_from_file("examples/policies_example.json"))
        justification_options_example = json.loads(get_example_from_file("examples/justification_options_example.json"))

        self.regional_policy = RegionalPolicy.objects.create(
            name="Test Policy",
            region=region,
            policies=policies_example,
            justification_options=justification_options_example,
            policy_title_text="Policy Title",
            policy_cancel_button_text="Cancel Button",
        )
        self.regional_policy.providers.set([self.provider])
        self.regional_policy.save()

    def test_create_regional_justification(self):

        request_data = {
            "justification_id": 1,
            "justification_suboption_value": "Option 1",
            "regional_policy_uid": str(self.regional_policy.uid),
        }

        url = reverse("api:regional_justifications-list")
        response = self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Content-Language"], "en")

        response = response.json()
        regional_justification = RegionalJustification.objects.last()
        self.assertEqual(regional_justification.justification_id, request_data["justification_id"])
        self.assertEqual(regional_justification.justification_name, "Justification Option with Dropdown Suboption")
        self.assertEqual(regional_justification.justification_suboption_value, "Option 1")
        self.assertEqual(regional_justification.regional_policy, self.regional_policy)

    def test_invalid_regional_policy(self):

        request_data = {
            "justification_id": 1,
            "justification_suboption_value": "Option 1",
            "regional_policy_uid": "invalid_uid",
        }

        url = reverse("api:regional_justifications-list")
        self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertRaisesMessage(
            Exception, "The Regional Policy for UID {request_data['regional_policy_uid']} does not exist."
        )

    def test_invalid_dropdown_suboption(self):

        request_data = {
            "justification_id": 1,
            "justification_suboption_value": "Invalid Option",
            "regional_policy_uid": str(self.regional_policy.uid),
        }

        url = reverse("api:regional_justifications-list")
        self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertRaisesMessage(ValidationError, "Invalid suboption selected.")

    def test_no_suboption_invalid_description(self):
        request_data = {
            "justification_id": 3,
            "justification_suboption_value": "Invalid Option",
            "regional_policy_uid": str(self.regional_policy.uid),
        }

        url = reverse("api:regional_justifications-list")
        self.client.post(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertRaisesMessage(
            ValidationError, "No suboption was available, so justification_suboption_value cannot be used."
        )
