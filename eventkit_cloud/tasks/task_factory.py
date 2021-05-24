# -*- coding: utf-8 -*-


import itertools
import logging
import os

from celery import chain
from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from eventkit_cloud.core.helpers import (
    sendnotification,
    NotificationVerb,
    NotificationLevel,
)
from eventkit_cloud.jobs.models import Job, JobPermission, JobPermissionLevel
from eventkit_cloud.tasks.export_tasks import (
    finalize_export_provider_task,
    TaskPriority,
    wait_for_providers_task,
    create_zip_task,
    finalize_run_task,
    output_selection_geojson_task,
    osm_data_collection_task,
    wfs_export_task,
    mapproxy_export_task,
    wcs_export_task,
    arcgis_feature_service_export_task,
    vector_file_export_task,
    raster_file_export_task,
    ogcapi_process_export_task,
)
from eventkit_cloud.tasks.enumerations import TaskState
from eventkit_cloud.tasks.helpers import (
    get_run_staging_dir,
    get_provider_staging_dir,
)
from eventkit_cloud.tasks.models import ExportRun, DataProviderTaskRecord
from eventkit_cloud.tasks.task_builders import (
    TaskChainBuilder,
    create_export_task_record,
)
from django.contrib.auth import get_user_model

User = get_user_model()

# Get an instance of a logger
logger = logging.getLogger(__name__)


class TaskFactory:
    """
    A class to assemble task chains (using TaskChainBuilders) based on an Export Run.
    """

    def __init__(
        self,
    ):
        self.type_task_map = {
            "osm": osm_data_collection_task,
            "wfs": wfs_export_task,
            "wms": mapproxy_export_task,
            "wcs": wcs_export_task,
            "wmts": mapproxy_export_task,
            "tms": mapproxy_export_task,
            "arcgis-raster": mapproxy_export_task,
            "arcgis-feature": arcgis_feature_service_export_task,
            "vector-file": vector_file_export_task,
            "raster-file": raster_file_export_task,
            "ogcapi-process": ogcapi_process_export_task,
        }

    def parse_tasks(
        self,
        worker=None,
        run_uid=None,
        user_details=None,
        data_provider_slugs=None,
        run_zip_file_slug_sets=None,
        session_token=None,
    ):
        """
        This handles all of the logic for taking the information about what individual celery tasks and groups
        them under specific providers.

        Each Provider (e.g. OSM) gets a chain:  OSM_TASK -> FORMAT_TASKS = PROVIDER_SUBTASK_CHAIN
        They need to be finalized (was the task successful?) to update the database state:
            PROVIDER_SUBTASK_CHAIN -> FINALIZE_PROVIDER_TASK

        We also have an optional chain of tasks that get processed after the providers are run:
            AD_HOC_TASK1 -> AD_HOC_TASK2 -> FINALIZE_RUN_TASK = FINALIZE_RUN_TASK_COLLECTION

        If the PROVIDER_SUBTASK_CHAIN fails it needs to be cleaned up.  The clean up task also calls the
        finalize provider task. This is because when a task fails the failed task will call an on_error (link_error)
        task and never return.
            PROVIDER_SUBTASK_CHAIN -> FINALIZE_PROVIDER_TASK
                   |
                   v
                CLEAN_UP_FAILURE_TASK -> FINALIZE_PROVIDER_TASK

        Now there needs to be someway for the finalize tasks to be called.  Since we now have several a possible
        forked path, we need each path to check the state of the providers to see if they are all finished before
        moving on.
        It would be great if celery would implicitly handled that, but it doesn't ever merge the forked paths.
        So we add a WAIT_FOR_PROVIDERS task to check state once the providers are ready they call the final tasks.

        PROVIDER_SUBTASK_CHAIN -> FINALIZE_PROVIDER_TASK -> WAIT_FOR_PROVIDERS   \
                   |                                                              ==> FINALIZE_RUN_TASK_COLLECTION
                   v                                                             /
            CLEAN_UP_FAILURE_TASK -> FINALIZE_PROVIDER_TASK -> WAIT_FOR_PROVIDERS


        :param worker: A worker node (hostname) for a celery worker, this should match the node name used when starting,
         the celery worker.
        :param run_uid: A uid to reference an ExportRun.
        :return: The AsyncResult from the celery chain of all tasks for this run.
        """
        # This is just to make it easier to trace when user_details haven't been sent
        if user_details is None:
            user_details = {"username": "TaskFactory-parse_tasks"}

        if run_uid:
            run = ExportRun.objects.get(uid=run_uid)
            job = run.job
            run_dir = get_run_staging_dir(run.uid)

            if not os.path.exists(run_dir):
                os.makedirs(run_dir, 0o750)

            queue_group = os.getenv("CELERY_GROUP_NAME", worker)
            wait_for_providers_settings = {
                "queue": f"{queue_group}.priority",
                "routing_key": f"{queue_group}.priority",
                "priority": TaskPriority.FINALIZE_PROVIDER.value,
            }

            finalize_task_settings = {
                "interval": 4,
                "max_retries": 10,
                "queue": f"{queue_group}.priority",
                "routing_key": f"{queue_group}.priority",
                "priority": TaskPriority.FINALIZE_RUN.value,
            }

            finalized_provider_task_chain_list = []
            # Create a task record which can hold tasks for the run (datapack)
            run_task_record = DataProviderTaskRecord.objects.create(
                run=run,
                name="run",
                slug="run",
                status=TaskState.PENDING.value,
                display=False,
            )
            stage_dir = get_provider_staging_dir(run_dir, run_task_record.slug)
            if not os.path.exists(stage_dir):
                os.makedirs(stage_dir, 0o750)

            run_zip_task_chain = get_zip_task_chain(
                data_provider_task_record_uid=run_task_record.uid,
                stage_dir=get_run_staging_dir(run_uid),
                worker=worker,
            )
            for provider_task in job.data_provider_tasks.all():
                # Skip any providers that weren't selected to be re-run.
                if data_provider_slugs and provider_task.provider.slug not in data_provider_slugs:
                    continue

                if self.type_task_map.get(provider_task.provider.export_provider_type.type_name):
                    # Each task builder has a primary task which pulls the source data, grab that task here...
                    type_name = provider_task.provider.export_provider_type.type_name

                    primary_export_task = self.type_task_map.get(type_name)

                    stage_dir = get_provider_staging_dir(run_dir, provider_task.provider.slug)
                    if not os.path.exists(stage_dir):
                        os.makedirs(stage_dir, 0o750)

                    args = {
                        "primary_export_task": primary_export_task,
                        "user": job.user,
                        "provider_task_uid": provider_task.uid,
                        "run": run,
                        "stage_dir": stage_dir,
                        "service_type": provider_task.provider.export_provider_type.type_name,
                        "worker": worker,
                        "user_details": user_details,
                        "session_token": session_token,
                    }

                    (
                        provider_task_record_uid,
                        provider_subtask_chain,
                    ) = TaskChainBuilder().build_tasks(**args)

                    wait_for_providers_signature = wait_for_providers_task.s(
                        run_uid=run_uid,
                        locking_task_key=run_uid,
                        callback_task=create_finalize_run_task_collection(
                            run_uid,
                            run_dir,
                            run_zip_task_chain,
                            run_zip_file_slug_sets=run_zip_file_slug_sets,
                            apply_args=finalize_task_settings,
                        ),
                        apply_args=finalize_task_settings,
                    ).set(**wait_for_providers_settings)

                    if provider_subtask_chain:
                        # The finalize_export_provider_task will check all of the export tasks
                        # for this provider and save the export provider's status.

                        selection_task = create_task(
                            data_provider_task_record_uid=provider_task_record_uid,
                            stage_dir=stage_dir,
                            worker=worker,
                            task=output_selection_geojson_task,
                            selection=job.the_geom.geojson,
                            user_details=user_details,
                        )

                        # create signature to close out the provider tasks
                        finalize_export_provider_signature = finalize_export_provider_task.s(
                            data_provider_task_uid=provider_task_record_uid,
                            status=TaskState.COMPLETED.value,
                            locking_task_key=run_uid,
                        ).set(**finalize_task_settings)

                        # add zip if required
                        if provider_task.provider.zip:
                            zip_export_provider_sig = get_zip_task_chain(
                                data_provider_task_record_uid=provider_task_record_uid,
                                data_provider_task_record_uids=[provider_task_record_uid],
                                stage_dir=stage_dir,
                                worker=worker,
                            )
                            provider_subtask_chain = chain(provider_subtask_chain, zip_export_provider_sig)

                        finalized_provider_task_chain_list.append(
                            chain(
                                selection_task,
                                provider_subtask_chain,
                                finalize_export_provider_signature,
                                wait_for_providers_signature,
                            )
                        )

            # we kick off all of the sub-tasks at once down here rather than one at a time in the for loop above so
            # that if an error occurs earlier on in the method, all of the tasks will fail rather than an undefined
            # number of them. this simplifies error handling, because we don't have to deduce which tasks were
            # successfully kicked off and which ones failed.
            for item in finalized_provider_task_chain_list:
                item.apply_async(**finalize_task_settings)


@transaction.atomic
def create_run(job_uid, user=None, clone=False):
    """
    This will create a new Run based on the provided job uid.
    :param job_uid: The UID to reference the Job model.
    :return: An ExportRun object.
    """

    # start the run
    try:
        # https://docs.djangoproject.com/en/1.10/topics/db/transactions/#django.db.transaction.atomic
        # enforce max runs
        with transaction.atomic():
            max_runs = settings.EXPORT_MAX_RUNS
            job = Job.objects.select_related("user").get(uid=job_uid)
            job, user = check_job_permissions(job, user)

            run_count = job.runs.filter(deleted=False).count()
            run_zip_file_slug_sets = None
            if clone:
                run, run_zip_file_slug_sets = job.last_export_run.clone()
                run.status = TaskState.SUBMITTED.value
                run.save()
                job.last_export_run = run
                job.save()
            else:
                # add the export run to the database
                run = ExportRun.objects.create(
                    job=job,
                    user=user,
                    status=TaskState.SUBMITTED.value,
                    expiration=(timezone.now() + timezone.timedelta(days=14)),
                )  # persist the run
                job.last_export_run = run
                job.save()

            if run_count > 0:
                while run_count > max_runs - 1:
                    # delete the earliest runs
                    job.runs.filter(deleted=False).earliest("started_at").soft_delete(user=user)
                    run_count -= 1

            sendnotification(
                run,
                run.user,
                NotificationVerb.RUN_STARTED.value,
                None,
                None,
                NotificationLevel.INFO.value,
                "",
            )
            run_uid = run.uid
            logger.debug("Saved run with id: {0}".format(str(run_uid)))
            if clone:
                return run_uid, run_zip_file_slug_sets
            else:
                return run_uid
    except DatabaseError as e:
        logger.error("Error saving export run: {0}".format(e))
        raise e


def check_job_permissions(job: Job, user: User = None) -> (Job, User):
    # get the number of existing runs for this job

    if not job.data_provider_tasks.all():
        raise Error(
            "This job does not have any data sources or formats associated with it, "
            "try cloning the job or submitting a new request."
        )
    invalid_licenses = get_invalid_licenses(job)
    if invalid_licenses:
        raise InvalidLicense(
            "The user: {0} has not agreed to the following licenses: {1}.\n"
            "Please use the user account page, or the user api to agree to the "
            "licenses prior to exporting the data.".format(job.user.username, invalid_licenses)
        )
    if not user:
        user = job.user

    jobs = JobPermission.userjobs(user, JobPermissionLevel.ADMIN.value)
    if not jobs.filter(id=job.id):
        raise Unauthorized(
            "The user: {0} is not authorized to create a run based on the job: {1}.".format(job.user.username, job.name)
        )

    return job, user


def create_task(
    data_provider_task_record_uid=None,
    data_provider_task_record_uids=None,
    run_zip_file_uid=None,
    stage_dir=None,
    worker=None,
    selection=None,
    task=None,
    job_name=None,
    user_details=None,
):
    """
    Create a new task to export the bounds for an DataProviderTaskRecord
    :param data_provider_task_uid: A uid for the dataprovidertaskrecord.
    :param stage_dir: The directory to store temporary files.
    :param worker: The hostname of the machine processing the task.
    :param selection: A geojson dict for the area being processed.
    :param task: The celery task to link to this task record.
    :param job_name: The name of the job, provided by the user.
    :param user_details: Some meta data relating to the user request.
    :return: A celery task signature.
    """
    # This is just to make it easier to trace when user_details haven't been sent
    if user_details is None:
        user_details = {"username": "unknown-create_task"}
    export_provider_task = DataProviderTaskRecord.objects.get(uid=data_provider_task_record_uid)

    if export_provider_task.provider:
        export_provider_task_slug = export_provider_task.provider.slug
    else:
        export_provider_task_slug = export_provider_task.slug

    export_task = create_export_task_record(
        task_name=task.name,
        export_provider_task=export_provider_task,
        worker=worker,
        display=getattr(task, "display", False),
    )
    queue_group = os.getenv("CELERY_GROUP_NAME", worker)
    return task.s(
        run_uid=export_provider_task.run.uid,
        task_uid=export_task.uid,
        selection=selection,
        stage_dir=stage_dir,
        provider_slug=export_provider_task_slug,
        data_provider_task_record_uid=data_provider_task_record_uid,
        data_provider_task_record_uids=data_provider_task_record_uids,
        run_zip_file_uid=run_zip_file_uid,
        job_name=job_name,
        user_details=user_details,
        bbox=export_provider_task.run.job.extents,
        locking_task_key=data_provider_task_record_uid,
    ).set(queue=queue_group, routing_key=queue_group)


def get_zip_task_chain(
    data_provider_task_record_uid=None,
    data_provider_task_record_uids=None,
    run_zip_file_uid=None,
    worker=None,
    stage_dir=None,
):
    return chain(
        create_task(
            data_provider_task_record_uid=data_provider_task_record_uid,
            data_provider_task_record_uids=data_provider_task_record_uids,
            run_zip_file_uid=run_zip_file_uid,
            stage_dir=stage_dir,
            worker=worker,
            task=create_zip_task,
        )
    )


def get_invalid_licenses(job, user=None):
    """
    :param user: A user to verify licenses against.
    :param job: The job containing the licensed datasets.
    :return: A list of invalid licenses.
    """
    from eventkit_cloud.api.serializers import UserDataSerializer

    user = user or job.user
    licenses = UserDataSerializer.get_user_accepted_licenses(user)
    invalid_licenses = []
    for provider_tasks in job.data_provider_tasks.all():
        license = provider_tasks.provider.license
        if license and not licenses.get(license.slug):
            invalid_licenses += [license.name]
    return invalid_licenses


class Error(Exception):
    def __init__(self, message):
        super(Exception, self).__init__(message)


class Unauthorized(Error):
    def __init__(self, message):
        super(Error, self).__init__("Unauthorized: {0}".format(message))


class InvalidLicense(Error):
    def __init__(self, message):
        super(Error, self).__init__("InvalidLicense: {0}".format(message))


def create_finalize_run_task_collection(
    run_uid=None, run_dir=None, run_zip_task_chain=None, run_zip_file_slug_sets=None, apply_args=None
):
    """Returns a 2-tuple celery chain of tasks that need to be executed after all of the export providers in a run
    have finished, and a finalize_run_task signature for use as an errback.
    """
    from eventkit_cloud.tasks.views import generate_zipfile_chain

    apply_args = apply_args or dict()

    # Use .si() to ignore the result of previous tasks, we just care that finalize_run_task runs last
    finalize_signature = finalize_run_task.si(run_uid=run_uid, stage_dir=run_dir).set(**apply_args)
    all_task_sigs_list = [run_zip_task_chain]

    if run_zip_file_slug_sets:
        for run_zip_file_slug_set in run_zip_file_slug_sets:
            zipfile_chain = generate_zipfile_chain(run_uid, run_zip_file_slug_set)
            if zipfile_chain:
                all_task_sigs_list.append(zipfile_chain)

    all_task_sigs_list.append(finalize_signature)
    all_task_sigs = itertools.chain(all_task_sigs_list)
    finalize_chain = chain(*all_task_sigs)

    return finalize_chain
