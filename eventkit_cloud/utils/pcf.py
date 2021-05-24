import requests
import os
import logging
from enum import Enum
import json

logger = logging.getLogger(__file__)
logging.basicConfig(level=logging.DEBUG)


class PcfTaskStates(Enum):
    SUCCEEDED = "SUCCEEDED"  # Used for runs when all tasks were successful
    RUNNING = "RUNNING"  # Used for runs when one or more tasks were unsuccessful
    FAILED = "FAILED"  # Used for runs that have not been started


class PcfClient(object):
    def __init__(self, api_url=None, org_name=None, space_name=None):
        self.api_url = os.getenv("PCF_API_URL", api_url)
        if not self.api_url:
            raise Exception("No api_url or PCF_API_URL provided.")
        self.session = requests.Session()
        self.info = None
        self.token = None
        self.org_name = org_name
        self.space_name = space_name
        self.org_guid = None
        self.space_guid = None

    def login(self, org_name=None, space_name=None):
        org_name = org_name or self.org_name
        space_name = space_name or self.space_name
        if not org_name and space_name:
            raise Exception("Both an org and space are required to login.")
        self.info = self.get_info()
        self.token = self.get_token()
        self.org_guid, self.org_name = self.get_org_guid(org_name=os.getenv("PCF_ORG", self.org_name))
        self.space_guid, self.space_name = self.get_space_guid(space_name=os.getenv("PCF_SPACE", self.space_name))

    def get_info(self):
        return self.session.get(
            "{0}/v2/info".format(self.api_url.rstrip("/")),
            headers={"Accept": "application/json"},
        ).json()

    def get_token(self):
        login_url = "{0}/login".format(self.info.get("authorization_endpoint").rstrip("/"))
        logger.debug(login_url)
        response = self.session.get(login_url)
        logger.debug(response.headers)
        user = os.getenv("PCF_USER")

        payload = {
            "grant_type": "password",
            "username": user,
            "password": os.getenv("PCF_PASS"),
            "scope": "",
            "response_type": "token",
        }
        url = "{0}/oauth/token".format(self.info.get("authorization_endpoint").rstrip("/"))
        logger.debug(url)
        response = self.session.post(
            url,
            data=payload,
            headers={
                "Authorization": "Basic Y2Y6",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        if response.status_code in ["401", "403"]:
            raise Exception("The user {0} and password provided were invalid.".format(user))
        token = response.json().get("access_token")
        if not token:
            raise Exception("Successfully logged into PCF but a token was not provided.")
        return token

    def get_org_guid(self, org_name):
        payload = {"order-by": "name"}
        url = "{0}/v2/organizations".format(self.api_url.rstrip("/"))
        response = self.session.get(
            url,
            data=payload,
            headers={
                "Authorization": "bearer {0}".format(self.token),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        organization_info = response.json()
        org_index = next(
            (
                index
                for (index, d) in enumerate(organization_info.get("resources"))
                if d["entity"].get("name", "").lower() == org_name.lower()
            ),
            None,
        )
        if org_index is None:
            raise Exception("Error the org {0} does not exist.".format(org_name))
        return organization_info["resources"][org_index]["metadata"]["guid"], org_name

    def get_space_guid(self, space_name):
        payload = {"order-by": "name", "inline-relations-depth": 1}
        url = "{0}/v2/organizations/{1}/spaces".format(self.api_url.rstrip("/"), self.org_guid)
        response = self.session.get(
            url,
            data=payload,
            headers={
                "Authorization": "bearer {0}".format(self.token),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        space_info = response.json()
        space_index = next(
            (
                index
                for (index, d) in enumerate(space_info.get("resources"))
                if d["entity"].get("name", "").lower() == space_name.lower()
            ),
            None,
        )
        if space_index is None:
            raise Exception("Error the space {0} does not exist in {1}.".format(space_name, self.org_name))
        return space_info["resources"][space_index]["metadata"]["guid"], space_name

    def get_app_guid(self, app_name):
        payload = {"q": "name:{0}".format(app_name), "inline-relations-depth": 1}
        url = "{0}/v2/spaces/{1}/apps".format(self.api_url.rstrip("/"), self.space_guid)
        response = self.session.get(
            url,
            data=payload,
            headers={
                "Authorization": "bearer {0}".format(self.token),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        app_info = response.json()
        app_index = next(
            (
                index
                for (index, d) in enumerate(app_info.get("resources"))
                if d["entity"].get("name", "").lower() == app_name.lower()
            ),
            None,
        )
        if app_index is None:
            raise Exception("Error the app {0} does not exist in {1}.".format(app_name, self.space_name))
        return app_info["resources"][app_index]["metadata"]["guid"]

    def run_task(self, name, command, disk_in_mb=None, memory_in_mb=None, app_name=None):
        app_name = (
            os.getenv(
                "PCF_APP",
                json.loads(os.getenv("VCAP_APPLICATION", "{}")).get("application_name"),
            )
            or app_name
        )
        if not app_name:
            raise Exception("An application name was not provided to run_task.")
        app_guid = self.get_app_guid(app_name)
        if not app_guid:
            raise Exception("An application guid could not be recovered for app {0}.".format(app_name))
        if not disk_in_mb:
            disk_in_mb = os.getenv("CELERY_TASK_DISK", "2048")
        if not memory_in_mb:
            memory_in_mb = os.getenv("CELERY_TASK_MEMORY", "2048")
        payload = {
            "name": name,
            "command": command,
            "disk_in_mb": disk_in_mb,
            "memory_in_mb": memory_in_mb,
        }
        url = "{0}/v3/apps/{1}/tasks".format(self.api_url.rstrip("/"), app_guid)
        return self.session.post(
            url,
            json=payload,
            headers={
                "Authorization": "bearer {0}".format(self.token),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        ).json()

    def get_running_tasks(self, app_name: str = None, names: str = None) -> list:
        """
        Get running pcf tasks.
        :param app_name: The name of the PCF app.
        :param names:  A comma separated list of names given to the tasks when they were originally called.
        :return: A list of the running task names.
        """
        app_name = (
            os.getenv(
                "PCF_APP",
                json.loads(os.getenv("VCAP_APPLICATION", "{}")).get("application_name"),
            )
            or app_name
        )
        if not app_name:
            raise Exception("An application name was not provided to get_running_tasks.")
        app_guid = self.get_app_guid(app_name)
        if not app_guid:
            raise Exception("An application guid could not be recovered for app {0}.".format(app_name))
        if names:
            payload = {
                "names": names,
                "states": PcfTaskStates.RUNNING.value,
            }
        else:
            payload = {
                "states": PcfTaskStates.RUNNING.value,
            }
        url = "{0}/v3/apps/{1}/tasks".format(self.api_url.rstrip("/"), app_guid)
        return self.session.get(
            url,
            params=payload,
            headers={"Authorization": "bearer {0}".format(self.token), "Accept": "application/json"},
        ).json()

    def get_running_tasks_memory(self, app_name: str) -> int:
        """
        Get running tasks memory for a single app.
        :param app_name: Name of app running tasks
        :return: Running task memory in mb.
        """

        running_tasks = self.get_running_tasks(app_name)
        running_tasks_memory = 0
        for task in running_tasks["resources"]:
            running_tasks_memory += task["memory_in_mb"]
        return running_tasks_memory
