# -*- coding: utf-8 -*-

import logging
import requests
from datetime import timedelta, datetime
import statistics
import json
import re
from time import sleep

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60 * 30  # 60 seconds * 30 (30 minutes)


class EventKitClient(object):
    def __init__(self, url, username=None, password=None, certificate=None, verify=True):
        self.base_url = url
        self.login_url = self.base_url + "/api/login/"
        self.cert_url = self.base_url + "/oauth"
        self.create_export_url = self.base_url + "/create"
        self.jobs_url = self.base_url + "/api/jobs"
        self.runs_url = self.base_url + "/api/runs"
        self.providers_url = self.base_url + "/api/providers"
        self.provider_tasks_url = self.base_url + "/api/provider_tasks"

        self.client = requests.session()
        self.client.verify = verify
        self.client.get(self.login_url)
        self.csrftoken = self.client.cookies.get("csrftoken")

        if certificate:
            self.client.get(self.cert_url, cert=certificate)
        elif username and password:
            login_data = dict(
                username=username,
                password=password,
                submit="Log in",
                csrfmiddlewaretoken=self.csrftoken,
                next="/",
            )
            self.client.post(
                self.login_url,
                data=login_data,
                headers=dict(Referer=self.login_url),
                auth=(username, password),
            )
        else:
            raise Exception("Unable to login without a certificate or username/password.")
        response = self.client.get(self.runs_url)
        if response.status_code in [401, 403]:
            raise Exception("Invalid Credentials were provided to EventKitClient")
        self.client.get(self.base_url)
        self.client.get(self.create_export_url)
        self.csrftoken = self.client.cookies.get("csrftoken")

    def get_providers(
        self,
    ):
        response = self.client.get(
            self.providers_url,
            headers={"X-CSRFToken": self.csrftoken, "Referer": self.create_export_url},
        )
        if response.status_code != 200:
            logger.error("There was an error getting the providers.")
            logger.error(response.text)
            raise Exception("Unable to get providers.")
        return response.json()

    def get_provider_task(self, uid):
        response = self.client.get(
            f"{self.provider_tasks_url.rstrip('/')}/{uid}",
            headers={"X-CSRFToken": self.csrftoken, "Referer": self.create_export_url},
        )
        if response.status_code != 200:
            logger.error("There was an error getting the providers.")
            logger.error(response.text)
            raise Exception("Unable to get providers.")
        return response.json()

    def search_runs(self, search_term=None):
        params = {"search_term": search_term}
        page = 1
        runs = []
        while True:
            params["page"] = page
            response = self.client.get(
                "{0}/filter".format(self.runs_url),
                params=params,
                headers={"X-CSRFToken": self.csrftoken, "Referer": self.create_export_url},
            )
            if response.ok:
                runs += response.json()
                page += 1
            else:
                # If the initial request was either empty or invalid raise an exception.
                if page == 1:
                    response.raise_for_status()
                else:
                    # We got some runs, but other requests failed. No more pages.
                    break
        return runs

    def get_runs(self, params=dict()):
        response = self.client.get(self.runs_url, params=params)
        if not response.ok:
            logger.info(response.content.decode())
            raise Exception("Could not search for runs with params: {}".format(params))
        return response.json()

    def create_job(
        self, name, description, project, selection, provider_tasks, tags=[], include_zipfile=True, **kwargs
    ):
        """
        :param name: A name for the datapack.
        :param description: A description for the datapack.
        :param project: A title for the project/event.
        :param selection: A geojson FeatureCollection representing the selection.
        :param provider_tasks: A list of providers (data sources).
           Example:
              [{
                "provider": "osm",
                "formats": ["shp", "gpkg"]
              }]
        :return:
        """
        data = {
            "name": name,
            "description": description,
            "event": project,
            "include_zipfile": include_zipfile,
            "selection": selection,
            "tags": [],
            "provider_tasks": provider_tasks,
        }
        response = self.client.post(
            self.jobs_url,
            json=data,
            headers={"X-CSRFToken": self.csrftoken, "Referer": self.create_export_url},
        )
        if response.status_code != 202:
            logger.error("There was an error creating the job: {0}".format(kwargs.get("name")))
            logger.error(response.content.decode())
            raise Exception("Unable to create Job.")
        return response.json()

    def rerun_job(self, job_uid):
        """
        :param job_uid: The Job UID to rerun.
        :return:
        """
        url = f"{self.jobs_url}/{job_uid}/run?format=json"

        response = self.client.post(
            url,
            headers={"X-CSRFToken": self.csrftoken, "Referer": self.create_export_url},
        )
        if not response.ok:
            logger.error(response.content.decode())
            logger.error(url)
        return response.json()

    def get_averages(self, runs):
        """

        :param runs: Runs is a list of runs provided by get_runs:
        :return: A dict with average time per provider per kilometer
           {
                "ProviderA": {"total": "duration",
                               "subtaskA", "duration"},
                "ProviderB": {"total": "duration",
                               "subtaskA", "duration"}
           }
        """
        providers = {}
        total_runs = len(runs)
        failed_runs = 0
        for run in runs:
            if run.get("status", "") != "COMPLETED":
                failed_runs += 1
            if not providers.get("runs"):
                providers["runs"] = {"times": [], "areas": []}
            area = self.get_area(run)
            providers["runs"]["areas"] += [area]
            duration = parse_duration(run.get("duration"))
            if duration:
                providers["runs"]["times"] += [duration / area]
            for pt in run.get("provider_tasks"):
                pt_name = pt["name"]
                if not providers.get(pt_name):
                    providers[pt_name] = {"times": [], "areas": []}
                if area:
                    providers[pt_name]["areas"] += [area]
                duration = parse_duration(pt.get("duration"))
                if duration:
                    providers[pt_name]["times"] += [duration / area]
                for task in pt["tasks"]:
                    task_name = task["name"]
                    if not providers.get(pt_name).get(task_name):
                        providers[pt_name][task_name] = {"times": []}
                    duration = parse_duration(task.get("duration"))
                    if duration:
                        providers[pt_name][task_name]["times"] += [duration / area]

        totals = {"total_runs": total_runs, "failed_runs": failed_runs}

        for provider in providers:
            totals[provider] = {}
            totals[provider]["count"] = len(providers[provider].get("times"))
            areas = providers[provider].pop("areas", None)
            if areas:
                totals[provider]["area_average"] = statistics.mean(areas)
            times = providers[provider].pop("times", None)
            if times:
                totals[provider]["total"] = convert_seconds_to_hms(statistics.mean(times))
            for task in providers[provider]:
                totals[provider][task] = {}
                times = providers[provider][task].get("times")
                if times:
                    totals[provider][task] = convert_seconds_to_hms(statistics.mean(times))
        return totals

    def get_area(self, run):
        if run["job"].get("area"):
            return run["job"].get("area")
        else:
            try:
                from osgeo import ogr
            except ImportError:
                logger.error("Cannot calculate statistics without knowing the area.")
                logger.error("This script needs a newer version of the API, or run with python that has gdal")
                raise
            geom = ogr.CreateGeometryFromJson(json.dumps(run["job"]["extent"]["geometry"]))
            geom = reproject(geom, 4326, 3857)
            return geom.GetArea() / 1000000

    def rerun_failed_runs(self, runs):
        for run in runs:
            if run.get("status", "") != "COMPLETED":
                self.rerun_job(run["job"]["uid"])

    def delete_run(self, run_uid):
        url = "{}/{}".format(self.runs_url.rstrip("/"), run_uid)
        response = self.client.delete(url, headers={"X-CSRFToken": self.csrftoken, "Referer": url})
        if response.status_code != 204:
            logger.info(response.status_code)
            logger.info(response.content.decode())
            raise Exception("Failed to properly delete run: {}".format(run_uid))

    def delete_job(self, job_uid):
        url = "{}/{}".format(self.jobs_url.rstrip("/"), job_uid)
        response = self.client.delete(url, headers={"X-CSRFToken": self.csrftoken, "Referer": url})
        if response.status_code != 204:
            logger.info(response.status_code)
            logger.info(response.content.decode())
            raise Exception("Failed to properly delete job: {}".format(job_uid))

    def cancel_provider(self, provider_uid):
        url = "{}/{}".format(self.provider_tasks_url.rstrip("/"), provider_uid)
        response = self.client.patch(url, headers={"X-CSRFToken": self.csrftoken, "Referer": url})
        if response.status_code != 200:
            logger.info(response.status_code)
            logger.info(response.content.decode())
            raise Exception("Failed to properly cancel provider task: {}".format(provider_uid))

    def wait_for_run(self, run_uid, run_timeout=DEFAULT_TIMEOUT):
        finished = False
        response = None
        first_check = datetime.now()
        errors = []
        while not finished:
            sleep(1)
            run_url = self.runs_url.rstrip("/"), run_uid
            logger.debug(run_url)
            response = self.client.get(
                "{}/{}".format(self.runs_url.rstrip("/"), run_uid),
                headers={"X-CSRFToken": self.csrftoken},
            )
            if not response.ok:
                logger.info(response.content.decode())
                raise Exception("Unable to get status of run {}".format(run_uid))
            response = response.json()
            status = response[0].get("status")
            if status in ["COMPLETED", "INCOMPLETE", "CANCELED"]:
                finished = True
            last_check = datetime.now()
            for run_details in response:
                for provider_task in run_details["provider_tasks"]:
                    for task in provider_task["tasks"]:
                        if task["status"] == "FAILED":
                            for error in task.get("errors", []):
                                for type, message in error.items():
                                    errors.append(f"{type}: {message}")
            if last_check - first_check > timedelta(seconds=run_timeout):
                raise Exception("Run timeout ({}s) exceeded".format(run_timeout))
        if errors:
            raise Exception("The run failed with errors: {}".format("\n".join(errors)))
        return response[0]

    def wait_for_task_pickup(self, job_uid):
        picked_up = False
        response = None
        while not picked_up:
            sleep(1)
            response = self.client.get(
                self.runs_url, params={"job_uid": job_uid}, headers={"X-CSRFToken": self.csrftoken}
            ).json()
            if response[0].get("provider_tasks"):
                picked_up = True
        return response[0]

    def check_provider(self, provider_slug):
        """
        :param provider_slug: The providuer slug (e.g. osm) to test.
        :return: True if provider status check is success or warn, else False.
        """

        url = "{}/{}/status".format(self.providers_url, provider_slug)
        response = self.client.get(url)
        if not response.ok:
            logger.error(response.content.decode())
            raise Exception("Failed to get the status of {}".format(provider_slug))
        response = response.json()
        if type(response) == str:
            # This is because the API is outputting the wrong type. It should return a JSON and proper error code.
            response = json.loads(response)
        if response.get("status") in ["SUCCESS", "WARN"]:
            return True
        return False


def parse_duration(duration):
    """
    returns seconds
    :param duration: A string ("hours:minutes:seconds")
    :return:
    """
    if duration:
        # Based off https://stackoverflow.com/questions/4628122/how-to-construct-a-timedelta-object-from-a-simple-string
        timedelta_regex = re.compile(
            r"^((?P<days>[\.\d]+?)\sday[s]?\,\s*)?((?P<hours>[\.\d]+?):)?((?P<minutes>[\.\d]+?):)?((?P<seconds>[\.\d]+?))?$"  # NOQA
        )

        parts = timedelta_regex.match(duration)
        if parts is not None:
            time_params = {name: float(param) for name, param in parts.groupdict().items() if param}
            return timedelta(**time_params).seconds


def convert_seconds_to_hms(seconds):
    """

    :param seconds: A float or int.
    :return: String "hours:minutes:seconds"
    """
    return str(timedelta(seconds=seconds))


def parse_byte_size(size, desired_unit="b"):
    """
    :param size: A string ("size unit" -- e.g. "256 MB")
    :param desired_unit: The desired output unit e.g. MB
    :return: sizeInBytes
    """
    if size:
        try:
            val, unit = size.split(" ")
            return float(val) * (parse_size_unit(unit) / parse_size_unit(desired_unit))
        except ValueError:
            return None


def parse_size_unit(unit):
    """
    :param unit: A string ("MB", "GB", "KB")
    :return: Number of bytes per specified unit e.g. GB=1e9
    """
    ul = unit.lower()

    if ul == "b":
        return 1
    if ul == "kb":
        return 1e3
    if ul == "mb":
        return 1e6
    if ul == "gb":
        return 1e9
    if ul == "tb":
        return 1e12

    raise ValueError("{} is an unknown unit".format(unit))


def reproject(geom, from_srs, to_srs):
    from osgeo import osr

    source = osr.SpatialReference()
    source.ImportFromEPSG(from_srs)
    target = osr.SpatialReference()
    target.ImportFromEPSG(to_srs)
    transform = osr.CoordinateTransformation(source, target)
    geom.Transform(transform)
    return geom
