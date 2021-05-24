# -*- coding: utf-8 -*-


import json
import os

from django.contrib.auth.models import Group, User
from django.contrib.gis.geos import GEOSGeometry, GeometryCollection, Polygon, Point, LineString
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.reverse import reverse
from rest_framework.test import APITestCase

from eventkit_cloud.core.models import GroupPermission, GroupPermissionLevel
from eventkit_cloud.jobs.models import ExportFormat, Job, DataProvider, DataProviderTask, DatamodelPreset
from eventkit_cloud.tasks.models import ExportRun


class TestJobPermissions(APITestCase):
    fixtures = (
        "osm_provider.json",
        "datamodel_presets.json",
    )

    def __init__(self, *args, **kwargs):
        super(TestJobPermissions, self).__init__(*args, **kwargs)
        self.path = None
        self.group = None
        self.user = None
        self.job = None
        self.client = None
        self.config = None
        self.tags = None

    def setUp(
        self,
    ):
        self.path = os.path.dirname(os.path.realpath(__file__))
        self.group, created = Group.objects.get_or_create(name="TestDefaultExportExtentGroup")
        self.user1 = User.objects.create_user(username="user_1", email="demo@demo.com", password="demo")
        self.user2 = User.objects.create_user(username="user_2", email="demo@demo.com", password="demo")

        extents = (-3.9, 16.1, 7.0, 27.6)
        bbox = Polygon.from_bbox(extents)
        original_selection = GeometryCollection(Point(1, 1), LineString((5.625, 48.458), (0.878, 44.339)))
        the_geom = GEOSGeometry(bbox, srid=4326)
        self.job = Job.objects.create(
            name="TestJob",
            event="Test Activation",
            description="Test description",
            user=self.user1,
            the_geom=the_geom,
            original_selection=original_selection,
        )

        formats = ExportFormat.objects.all()
        provider = DataProvider.objects.first()
        provider_task = DataProviderTask.objects.create(provider=provider)
        provider_task.formats.add(*formats)

        self.job.data_provider_tasks.add(provider_task)

        token = Token.objects.create(user=self.user1)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )
        # create a test config
        hdm_presets = DatamodelPreset.objects.get(name="hdm")
        self.job.preset = hdm_presets
        self.job.save()

        group1, created = Group.objects.get_or_create(name="group_one")
        self.group1id = group1.id
        GroupPermission.objects.create(group=group1, user=self.user1, permission=GroupPermissionLevel.ADMIN.value)
        GroupPermission.objects.create(group=group1, user=self.user2, permission=GroupPermissionLevel.MEMBER.value)
        group2, created = Group.objects.get_or_create(name="group_two")
        self.group2id = group2.id
        GroupPermission.objects.create(group=group2, user=self.user1, permission=GroupPermissionLevel.ADMIN.value)
        GroupPermission.objects.create(group=group2, user=self.user2, permission=GroupPermissionLevel.MEMBER.value)

    def test_list(
        self,
    ):
        expected = "/api/jobs"
        url = reverse("api:jobs-list")
        self.assertEqual(expected, url)

    def test_get_job_defaults(
        self,
    ):
        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        self.assertEqual(expected, url)
        response = self.client.get(url)

        self.assertEqual(response.data["visibility"], "PRIVATE")
        self.assertEqual(len(response.data["permissions"]["members"]), 1)
        self.assertEqual(len(response.data["permissions"]["groups"]), 0)
        self.assertEqual(self.user1.username in response.data["permissions"]["members"], True)
        self.assertEqual(response.data["permissions"]["members"][self.user1.username], "ADMIN")
        self.assertEqual(response.data["visibility"], "PRIVATE")

    def test_permissions_syntax(
        self,
    ):
        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        self.assertEqual(expected, url)

        request_data = {
            "permissions": {"members": {"user_1": "ADMIN", "user_2": "ADMIN"}, "groups": {"group_one": "ADMIN"}}
        }

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        request_data = {"permissions": {"members": {"user_1": "READ", "user_2": "READ"}, "groups": {}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response.data[0]["detail"], "Cannot update job permissions with no administrator.")

        request_data = {"permissions": {"members": {"user_1": "ADMIN", "user_2": "ADMEN"}, "groups": {}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response.data[0]["detail"], "invalid permission value : ADMEN")

        request_data = {"permissions": {"members": {"user_1": "ADMIN", "user_3": "ADMIN"}, "groups": {}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response.data[0]["detail"], "unidentified user or group : user_3")

        request_data = {"permissions": {"members": {"user_1": "ADMIN"}, "groups": {"group_three": "ADMIN"}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response.data[0]["detail"], "unidentified user or group : group_three")

        request_data = {"permissions": {"members": {"user_1": "ADMIN"}, "groups": {"group_two": "ADMEN"}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response.data[0]["detail"], "invalid permission value : ADMEN")

    def test_shared_user_permissions(
        self,
    ):
        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        self.assertEqual(expected, url)

        request_data = {"permissions": {"members": {"user_1": "ADMIN", "user_2": "ADMIN"}, "groups": {}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        # change user_1 permissions to READ and visiblity SHARED

        request_data = {
            "visibility": "SHARED",
            "permissions": {"members": {"user_1": "READ", "user_2": "ADMIN"}, "groups": {}},
        }

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        # Update should no longer be permitted

        request_data = {"visibility": "PRIVATE"}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response.data[0]["detail"], "ADMIN permission is required to update this Datapack.")

        # however, should be able to read this job.

        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        response = self.client.get(url)

        self.assertEqual(response.data["visibility"], "SHARED")
        self.assertEqual(response.data["permissions"]["members"][self.user1.username], "READ")

    def test_private_user_permissions(
        self,
    ):
        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        self.assertEqual(expected, url)

        request_data = {"permissions": {"members": {"user_1": "ADMIN", "user_2": "ADMIN"}, "groups": {}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        #  remove user_1 and set visibility PRIVATE.

        request_data = {"visibility": "PRIVATE", "permissions": {"members": {"user_2": "ADMIN"}, "groups": {}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        # Update should no longer be permitted

        request_data = {"published": True}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response.data[0]["detail"], "ADMIN permission is required to update this Datapack.")

        #  should not be able to read this job

        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        response = self.client.get(url)
        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)

    def test_public_user_permissions(
        self,
    ):
        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        self.assertEqual(expected, url)

        request_data = {"permissions": {"members": {"user_1": "ADMIN", "user_2": "ADMIN"}, "groups": {}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        #  remove user_1 and set visibility PUBLIC.

        request_data = {"visibility": "PUBLIC", "permissions": {"members": {"user_2": "ADMIN"}, "groups": {}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        # Update should no longer be permitted

        request_data = {"published": True}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response.data[0]["detail"], "ADMIN permission is required to update this Datapack.")

        #  should be able to read this job

        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        response = self.client.get(url)

        self.assertEqual(response.data["visibility"], "PUBLIC")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

    def test_shared_group_permissions(
        self,
    ):
        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        self.assertEqual(expected, url)

        # remove logged in user as an administrator, allow his group to be ADMIN

        request_data = {"permissions": {"members": {"user_2": "ADMIN"}, "groups": {"group_one": "ADMIN"}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        # Update should be OK

        request_data = {"visibility": "SHARED"}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        #  should be able to read this job.

        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        response = self.client.get(url)

        self.assertEqual(response.data["permissions"]["groups"]["group_one"], "ADMIN")

        #  Now change the group's permissions to READ effectively removing user_1 from any admin rights

        request_data = {"permissions": {"members": {"user_2": "ADMIN"}, "groups": {"group_one": "READ"}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        # update should now fail. Here we try to regain our lost ADMIN rights

        request_data = {"permissions": {"members": {"user_2": "ADMIN"}, "groups": {"group_one": "ADMIN"}}}

        response = self.client.patch(url, data=json.dumps(request_data), content_type="application/json; version=1.0")
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertEqual(response.data[0]["detail"], "ADMIN permission is required to update this Datapack.")

        #  should still be able to read this job.

        expected = "/api/jobs/{0}".format(self.job.uid)
        url = reverse("api:jobs-detail", args=[self.job.uid])
        response = self.client.get(url)

        self.assertEqual(response.data["permissions"]["groups"]["group_one"], "READ")


class TestExportRunViewSet(APITestCase):
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
        self.user1 = User.objects.create_user(username="user_1", email="demo@demo.com", password="demo")
        self.user2 = User.objects.create_user(username="user_2", email="demo@demo.com", password="demo")
        token = Token.objects.create(user=self.user1)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )
        extents = (-3.9, 16.1, 7.0, 27.6)
        bbox = Polygon.from_bbox(extents)
        the_geom = GEOSGeometry(bbox, srid=4326)
        self.job = Job.objects.create(
            name="TestJob", description="Test description", user=self.user1, the_geom=the_geom
        )
        formats = ExportFormat.objects.all()
        provider = DataProvider.objects.first()
        provider_task = DataProviderTask.objects.create(provider=provider)
        provider_task.formats.add(*formats)

        self.job.data_provider_tasks.add(provider_task)
        self.job.save()
        self.job_uid = str(self.job.uid)
        self.export_run = ExportRun.objects.create(job=self.job, user=self.user1)
        self.run_uid = str(self.export_run.uid)

        self.group1, created = Group.objects.get_or_create(name="group_one")
        GroupPermission.objects.create(group=self.group1, user=self.user1, permission=GroupPermissionLevel.ADMIN.value)
        GroupPermission.objects.create(group=self.group1, user=self.user2, permission=GroupPermissionLevel.MEMBER.value)
        self.group2, created = Group.objects.get_or_create(name="group_two")
        GroupPermission.objects.create(group=self.group2, user=self.user1, permission=GroupPermissionLevel.ADMIN.value)
        self.job_url = reverse("api:jobs-detail", args=[self.job.uid])
        self.run_url = reverse("api:runs-detail", args=[self.run_uid])

    def assert_user_access(self, user):

        token = Token.objects.create(user=user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

        url = reverse("api:runs-detail", args=[self.run_uid])
        response = self.client.get(url)
        result = response.data
        self.assertEqual(self.run_uid, result[0].get("uid"))

    def test_retrieve_run(
        self,
    ):
        expected = "/api/runs/{0}".format(self.run_uid)

        self.assertEqual(expected, self.run_url)
        response = self.client.get(self.run_url)
        self.assertIsNotNone(response)
        result = response.data
        # make sure we get the correct uid back out
        self.assertEqual(self.run_uid, result[0].get("uid"))

    def test_private_run(
        self,
    ):
        token = Token.objects.create(user=self.user2)
        self.client.credentials(
            HTTP_AUTHORIZATION="Token " + token.key,
            HTTP_ACCEPT="application/json; version=1.0",
            HTTP_ACCEPT_LANGUAGE="en",
            HTTP_HOST="testserver",
        )

        response = self.client.get(self.run_url)
        result = response.data
        self.assertEqual(result, [])

    def test_public_sharing(
        self,
    ):

        joburl = reverse("api:jobs-detail", args=[self.job.uid])

        request_data = {
            "permissions": {"value": "PUBLIC", "members": {self.user1.username: "ADMIN"}, "groups": {}},
            "visibility": "PUBLIC",
        }

        response = self.client.patch(
            joburl, data=json.dumps(request_data), content_type="application/json; version=1.0"
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        self.assert_user_access(self.user2)

    def test_user_sharing(
        self,
    ):
        request_data = {"permissions": {"members": {self.user1.username: "ADMIN", self.user2.username: "ADMIN"}}}

        response = self.client.patch(
            self.job_url, data=json.dumps(request_data), content_type="application/json; version=1.0"
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        self.assert_user_access(self.user2)

        request_data = {"permissions": {"members": {self.user1.username: "ADMIN"}}}

        response = self.client.patch(
            self.job_url, data=json.dumps(request_data), content_type="application/json; version=1.0"
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        response = self.client.get(self.run_url)
        result = response.data
        self.assertEqual(result, [])

    def test_group_sharing(
        self,
    ):

        request_data = {"permissions": {"groups": {self.group1.name: "ADMIN"}}}

        response = self.client.patch(
            self.job_url, data=json.dumps(request_data), content_type="application/json; version=1.0"
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        self.assert_user_access(self.user2)

        request_data = {"permissions": {"groups": {self.group2.name: "ADMIN"}}}

        response = self.client.patch(
            self.job_url, data=json.dumps(request_data), content_type="application/json; version=1.0"
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        response = self.client.get(self.run_url)
        result = response.data
        self.assertEqual(result, [])
