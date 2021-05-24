import requests
import time

from eventkit_cloud.utils import auth_requests, gdalutils
from eventkit_cloud.auth.views import has_valid_access_token
from eventkit_cloud.tasks.enumerations import OGC_Status
from eventkit_cloud.tasks.task_process import update_progress
from django.conf import settings
from urllib.parse import urljoin


class OgcApiProcess:
    def __init__(self, url, config, session_token, task_id, *args, **kwargs):
        self.base_url = url.rstrip("/\\")
        self.base_url += "/"
        self.config = validate_ogc_config(config)
        self.task_id = task_id

        valid_token = has_valid_access_token(session_token)
        if not valid_token:
            raise Exception("Invalid access token.")
        self.session = auth_requests.AuthSession().session
        self.session.headers.update({"Authorization": f"Bearer: {session_token}"})

    def create_job(self, bbox):
        payload = get_job_payload(
            process_id=self.config.get("process"),
            product=self.config.get("inputs", dict()).get("product"),
            bbox=bbox,
            file_format=self.config.get("inputs", dict()).get("file_format"),
            archive_format=self.config.get("outputs", dict()).get("archive_format"),
        )

        jobs_endpoint = urljoin(self.base_url, "jobs/")

        try:
            response = self.session.post(
                jobs_endpoint,
                json=payload,
                verify=getattr(settings, "SSL_VERIFICATION", True),
            )
            response.raise_for_status()

        except requests.exceptions.RequestException as e:
            raise Exception(f"Unsuccessful request:{e}")

        response_content = response.json()
        job_id = response_content.get("jobID")
        if not job_id:
            raise Exception("Invalid response from OGC API server")

        self.job_url = urljoin(jobs_endpoint, f"{job_id}/")

        return response_content

    def get_job_results(
        self,
    ):
        """
        Fetches the job results
        Returns the results' download URL.
        """

        # poll job status
        ogc_job = self.wait_for_ogc_process_job(self.job_url, self.task_id)
        if ogc_job.get("status") != OGC_Status.SUCCESSFUL.value:
            raise Exception(f"Unsuccessful OGC export. {ogc_job.get('status')}: {ogc_job.get('message')}")

        update_progress(self.task_id, progress=50, subtask_percentage=50)

        # fetch job results
        try:
            response = self.session.get(
                urljoin(self.job_url, "results/"),
                verify=getattr(settings, "SSL_VERIFICATION", True),
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise Exception(f"Unsuccessful request:{e}")

        response_content = response.json()
        download_url = response_content.get("archive_format", dict()).get("href")

        if not download_url:
            raise Exception("Invalid response from OGC API server")

        return download_url

    @gdalutils.retry
    def wait_for_ogc_process_job(self, job_url, task_id=None, interval=5):
        """
        Function polls an OGC process' job until it is done processing.
        Returns the final response's content, which includes the job' status.
        """

        job_status = None
        while job_status not in OGC_Status.get_finished_status():
            time.sleep(interval)
            try:
                response = self.session.get(
                    job_url,
                    verify=getattr(settings, "SSL_VERIFICATION", True),
                )
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                raise Exception(f"Unsuccessful request:{e}")
            response_content = response.json()
            job_status = response_content.get("status")
            if not job_status:
                raise Exception("Invalid response from OGC API server")
            if task_id:
                update_progress(task_id, progress=25, subtask_percentage=response_content.get("progress", 50))

        if job_status in OGC_Status.get_finished_status():
            return response_content


def validate_ogc_config(config: dict()):
    """
    Function takes data provider configuration.
    Ensures config has all the necessary parametres needed to create an OGC API Process job
    Returns dictionary containing only the values of "ogcapi_process" if present.
    """
    try:
        ogc_config = config.get("ogcapi_process")
        if ogc_config is None:
            raise Exception()

        inputs = ogc_config.get("inputs")
        outputs = ogc_config.get("outputs")
        process = ogc_config.get("process")

        if None in [inputs, outputs, process]:
            raise Exception()

        if None in [
            inputs.get("product"),
            inputs.get("file_format"),
            outputs.get("archive_format"),
        ]:
            raise Exception()

    except Exception as e:
        raise Exception(f"Invalid OGC API Process provider configuration: {e}")

    return ogc_config


def get_job_payload(
    process_id: str, product: str, bbox, file_format: str = "gpkg", archive_format: str = "application/zip"
):
    """
    Function generates the request body needed for a POST request to the OGC /jobs endpoint.
    """
    payload = {
        "id": process_id,
        "inputs": {"product": {"value": product}, "file_format": {"value": file_format}, "bbox": {"bbox": bbox}},
        "outputs": {"archive_format": {"format": {"mediaType": archive_format}, "transmissionMode": "reference"}},
        "mode": "auto",
        "response": "document",
    }

    return payload
