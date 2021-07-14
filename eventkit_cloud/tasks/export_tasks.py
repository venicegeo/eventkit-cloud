# -*- coding: utf-8 -*-

import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import time
import traceback
from pathlib import Path
from typing import List
from urllib.parse import urlencode, urljoin
from zipfile import ZipFile, ZIP_DEFLATED

import yaml
from audit_logging.celery_support import UserDetailsBase
from billiard.einfo import ExceptionInfo
from billiard.exceptions import SoftTimeLimitExceeded
from celery import signature
from celery.result import AsyncResult
from celery.utils.log import get_task_logger
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMultiAlternatives
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.template.loader import get_template
from django.utils import timezone

from eventkit_cloud.celery import app, TaskPriority
from eventkit_cloud.core.helpers import (
    sendnotification,
    NotificationVerb,
    NotificationLevel,
)
from eventkit_cloud.feature_selection.feature_selection import FeatureSelection
from eventkit_cloud.jobs.enumerations import GeospatialDataType
from eventkit_cloud.jobs.models import DataProviderTask, DataProvider, ExportFormat, load_provider_config, clean_config
from eventkit_cloud.tasks import set_cache_value
from eventkit_cloud.tasks.enumerations import TaskState
from eventkit_cloud.tasks.exceptions import CancelException, DeleteException
from eventkit_cloud.tasks.helpers import (
    Directory,
    PREVIEW_TAIL,
    add_export_run_files_to_zip,
    check_cached_task_failures,
    generate_qgs_style,
    get_arcgis_metadata,
    get_archive_data_path,
    get_data_package_manifest,
    get_download_filename,
    get_export_filepath,
    get_human_readable_metadata_document,
    get_metadata,
    get_provider_download_dir,
    get_export_task_record,
    get_provider_staging_dir,
    get_provider_staging_preview,
    get_run_download_dir,
    get_run_download_url,
    get_run_staging_dir,
    get_style_files,
    normalize_name,
    pickle_exception,
    progressive_kill,
    download_data,
    download_concurrently,
    merge_chunks,
    find_in_zip,
    get_celery_queue_group,
    extract_metadata_files,
    get_geometry,
    update_progress,
)
from eventkit_cloud.tasks.metadata import metadata_tasks
from eventkit_cloud.tasks.models import (
    ExportTaskRecord,
    ExportTaskException,
    DataProviderTaskRecord,
    FileProducingTaskResult,
    ExportRun,
    RunZipFile,
)
from eventkit_cloud.tasks.task_base import EventKitBaseTask
from eventkit_cloud.tasks.util_tasks import shutdown_celery_workers, rerun_data_provider_records
from eventkit_cloud.utils import overpass, pbf, s3, mapproxy, wcs, geopackage, gdalutils, auth_requests
from eventkit_cloud.utils.client import EventKitClient
from eventkit_cloud.utils.ogcapi_process import OgcApiProcess, get_format_field_from_config
from eventkit_cloud.utils.qgis_utils import convert_qgis_gpkg_to_kml
from eventkit_cloud.utils.rocket_chat import RocketChat
from eventkit_cloud.utils.stats.eta_estimator import ETA

BLACKLISTED_ZIP_EXTS = [".ini", ".om5", ".osm", ".lck", ".pyc"]

# Get an instance of a logger
logger = get_task_logger(__name__)


# http://docs.celeryproject.org/en/latest/tutorials/task-cookbook.html
# https://github.com/celery/celery/issues/3270


def make_file_downloadable(filepath, run_uid, provider_slug=None, skip_copy=False, download_filename=None):
    """ Construct the filesystem location and url needed to download the file at filepath.
        Copy filepath to the filesystem location required for download.
        @provider_slug is specific to ExportTasks, not needed for FinalizeHookTasks
        @skip_copy: It looks like sometimes (At least for OverpassQuery) we don't want the file copied,
            generally can be ignored
        @return A url to reach filepath.
    """

    if provider_slug:
        staging_dir = get_provider_staging_dir(run_uid, provider_slug)
        run_download_dir = get_provider_download_dir(run_uid, provider_slug)
    else:
        staging_dir = get_run_staging_dir(run_uid)
        run_download_dir = get_run_download_dir(run_uid)

    run_download_url = get_run_download_url(run_uid, provider_slug)
    filename = os.path.basename(filepath)
    if download_filename is None:
        download_filename = filename

    if getattr(settings, "USE_S3", False):
        source_path = os.path.join(staging_dir, filename)
        if provider_slug:
            download_filepath = os.path.join(run_uid, provider_slug, download_filename)
        else:
            download_filepath = os.path.join(run_uid, download_filename)
        download_url = s3.upload_to_s3(source_path, download_filepath)
    else:
        make_dirs(run_download_dir)

        download_url = os.path.join(run_download_url, download_filename)

        download_filepath = os.path.join(run_download_dir, download_filename)
        if not skip_copy:
            shutil.copy(filepath, download_filepath)
    return download_url


# ExportTaskRecord abstract base class and subclasses.
class ExportTask(EventKitBaseTask):
    """
    Abstract base class for export tasks.
    """

    # whether to abort the whole provider if this task fails.
    abort_on_error = False
    name = "ExportTask"
    display = True
    hide_download = True

    def __call__(self, *args, **kwargs) -> dict:
        task_uid = kwargs.get("task_uid")

        try:
            task = (
                ExportTaskRecord.objects.select_related("export_provider_task__run__job")
                .select_related("export_provider_task__provider")
                .get(uid=task_uid)
            )
            if self.hide_download:
                task.hide_download = True

            check_cached_task_failures(task.name, task_uid)

            task.started_at = timezone.now()
            task.worker = socket.gethostname()
            task.save()

            run = task.export_provider_task.run
            run_dir = get_run_staging_dir(run.uid)

            if not os.path.exists(run_dir):
                os.makedirs(run_dir, 0o750)

            run_task_record = run.data_provider_task_records.get(slug="run")

            # Returns the default only if the key does not exist in the dictionary.
            stage_dir = kwargs.get("stage_dir", get_provider_staging_dir(run_dir, run_task_record.slug))

            # Check for None because the above statement could return None.
            if stage_dir is None:
                stage_dir = get_provider_staging_dir(run_dir, run_task_record.slug)
            kwargs["stage_dir"] = stage_dir

            if not os.path.exists(stage_dir):
                os.makedirs(stage_dir, 0o750)

            try:
                task_state_result = args[0]
            except IndexError:
                task_state_result = None
            self.update_task_state(result=task_state_result, task_uid=task_uid)

            if TaskState.CANCELED.value not in [task.status, task.export_provider_task.status]:
                try:
                    retval = super(ExportTask, self).__call__(*args, **kwargs)
                except SoftTimeLimitExceeded as e:
                    logger.error(e)
                    raise Exception("Task time limit exceeded. Try again or contact us.") from e

            """
            Update the successfully completed task as follows:

                1. update the time the task completed
                2. calculate the size of the output file
                3. calculate the download path of the export
                4. create the export download directory
                5. copy the export file to the download directory
                6. create the export task result
                7. update the export task status and save it
                """
            # If a task is skipped it will be successfully completed but it won't have a return value.
            # Something needs to be populated to notify the user and to skip the following steps.
            if not (retval and retval.get("result")):
                raise Exception("This task was skipped due to previous failures/cancellations.")

            try:
                add_metadata(task.export_provider_task.run.job, task.export_provider_task.provider, retval)
            except Exception:
                logger.error(traceback.format_exc())
                logger.error("Failed to add metadata.")

            # update the task
            finished = timezone.now()
            if TaskState.CANCELED.value in [task.status, task.export_provider_task.status]:
                logging.info("Task reported on success but was previously canceled ", format(task_uid))
                username = None
                if task.cancel_user:
                    username = task.cancel_user.username
                raise CancelException(task_name=task.export_provider_task.name, user_name=username)

            task.finished_at = finished
            task.progress = 100
            task.pid = -1
            # get the output
            output_url = retval["result"]
            stat = os.stat(output_url)
            size = stat.st_size / 1024 / 1024.00
            # construct the download_path
            parts = output_url.split("/")
            filename = parts[-1]
            provider_slug = parts[-2]
            run_uid = parts[-3]
            name, ext = os.path.splitext(filename)
            if provider_slug == "run":
                event = normalize_name(task.export_provider_task.run.job.event)
                download_filename = get_download_filename(name, ext, additional_descriptors=[event, "eventkit"])
            else:
                download_filename = get_download_filename(name, ext)

            # construct the download url
            skip_copy = task.name == "OverpassQuery"
            download_url = make_file_downloadable(
                output_url,
                run_uid,
                provider_slug=provider_slug,
                skip_copy=skip_copy,
                download_filename=download_filename,
            )

            # save the task and task result
            result = FileProducingTaskResult.objects.create(filename=filename, size=size, download_url=download_url)

            task.result = result
            task.status = TaskState.SUCCESS.value
            task.save()
            retval["status"] = TaskState.SUCCESS.value
            retval["file_producing_task_result_id"] = result.id
            return retval
        except CancelException:
            return {"status": TaskState.CANCELED.value}
        except Exception as e:
            tb = traceback.format_exc()
            logger.error("Exception in the handler for {}:\n{}".format(self.name, tb))
            from billiard.einfo import ExceptionInfo

            einfo = ExceptionInfo()
            result = self.task_failure(e, task_uid, args, kwargs, einfo)
            return result

    @transaction.atomic
    def task_failure(self, exc, task_id, args, kwargs, einfo):
        """
        Update the failed task as follows:

            1. pull out the ExportTaskRecord
            2. update the task status and finish time
            3. create an export task exception
            4. save the export task with the task exception
            5. run export_task_error_handler if the run should be aborted
               - this is only for initial tasks on which subsequent export tasks depend
        """
        # TODO: If there is a failure before the task was created this will fail to run.
        status = TaskState.FAILED.value
        try:
            export_task_record = ExportTaskRecord.objects.select_related("export_provider_task__run").get(uid=task_id)
            export_task_record.finished_at = timezone.now()
            export_task_record.save()
        except Exception:
            logger.error(traceback.format_exc())
            logger.error(
                "Cannot update the status of ExportTaskRecord object: no such object has been created for "
                "this task yet."
            )
            return {"status": status}
        ete = ExportTaskException(task=export_task_record, exception=pickle_exception(einfo))
        ete.save()
        if export_task_record.status == TaskState.CANCELED.value:
            status = TaskState.CANCELED.value
        export_task_record.status = status
        export_task_record.save()
        logger.debug("Task name: {0} failed, {1}".format(self.name, einfo))
        if self.abort_on_error or True:
            try:
                data_provider_task_record = export_task_record.export_provider_task
                fail_synchronous_task_chain(data_provider_task_record=data_provider_task_record)
                run = data_provider_task_record.run
                stage_dir = kwargs["stage_dir"]
                export_task_error_handler(run_uid=str(run.uid), task_id=task_id, stage_dir=stage_dir)
            except Exception:
                tb = traceback.format_exc()
                logger.error("Exception during handling of an error in {}:\n{}".format(self.name, tb))
        return {"status": status}

    def update_task_state(self, result=None, task_status=TaskState.RUNNING.value, task_uid=None):
        """
        Update the task state and celery task uid.
        Can use the celery uid for diagnostics.
        """
        result = result or {}
        started = timezone.now()

        try:
            task = ExportTaskRecord.objects.get(uid=task_uid)
            celery_uid = self.request.id
            if not celery_uid:
                raise Exception("Failed to save celery_UID")
            task.celery_uid = celery_uid
            task.save()
            result = parse_result(result, "status") or []
            if TaskState.CANCELED.value in [task.status, task.export_provider_task.status, result]:
                logging.info("canceling before run %s", celery_uid)
                task.status = TaskState.CANCELED.value
                task.save()
                raise CancelException(task_name=task.export_provider_task.name)
            # The parent ID is actually the process running in celery.
            task.pid = os.getppid()
            task.status = task_status
            task.export_provider_task.status = TaskState.RUNNING.value
            task.started_at = started
            task.save()
            task.export_provider_task.save()
            logger.debug("Updated task: {0} with uid: {1}".format(task.name, task.uid))
        except DatabaseError as e:
            logger.error("Updating task {0} state throws: {1}".format(task_uid, e))
            raise e


class AbortOnErrorTask(ExportTask):
    abort_on_error = True


class FormatTask(ExportTask):
    """
    A class to manage tasks which are desired output from the user, and not merely associated files or metadata.
    """

    name = "FormatTask"
    display = True
    hide_download = False


class ZipFileTask(FormatTask):
    def __call__(self, *args, **kwargs):
        run = ExportRun.objects.get(uid=kwargs["run_uid"])
        if kwargs["run_zip_file_uid"]:
            run_zip_file = RunZipFile.objects.get(uid=kwargs["run_zip_file_uid"])
            run_zip_file.run = run
        else:
            run_zip_file = RunZipFile.objects.create(run=run)
            kwargs["run_zip_file_uid"] = run_zip_file.uid

        retval = super(ZipFileTask, self).__call__(*args, **kwargs)

        if retval["status"] == "CANCELED":
            return retval

        if not kwargs["data_provider_task_record_uids"]:
            data_provider_task_record = DataProviderTaskRecord.objects.get(uid=kwargs["data_provider_task_record_uid"])
            data_provider_task_records = data_provider_task_record.run.data_provider_task_records.exclude(slug="run")
            data_provider_task_record_uids = [
                data_provider_task_record.uid for data_provider_task_record in data_provider_task_records
            ]
        else:
            data_provider_task_record_uids = kwargs["data_provider_task_record_uids"]

        if retval.get("file_producing_task_result_id"):
            data_provider_task_records = DataProviderTaskRecord.objects.filter(uid__in=data_provider_task_record_uids)
            downloadable_file = FileProducingTaskResult.objects.get(id=retval["file_producing_task_result_id"])
            run_zip_file.downloadable_file = downloadable_file
            run_zip_file.data_provider_task_records.set(data_provider_task_records)
            run_zip_file.finished_at = timezone.now()
            run_zip_file.message = "Completed"
            run_zip_file.save()
        return retval


@gdalutils.retry
def osm_data_collection_pipeline(
    export_task_record_uid,
    stage_dir,
    job_name="no_job_name_specified",
    url=None,
    slug=None,
    bbox=None,
    selection=None,
    user_details=None,
    config=None,
    eta=None,
    projection=4326,
) -> dict:
    """
    Collects data from OSM & produces a thematic gpkg as a subtask of the task referenced by export_provider_task_id.
    bbox expected format is an iterable of the form [ long0, lat0, long1, lat1 ]
    """

    if config is None:
        logger.error("No configuration was provided for OSM export")
        raise RuntimeError("The configuration field is required for OSM data providers")

    pbf_file = yaml.load(config).get("pbf_file")

    if pbf_file:
        logger.info(f"Using PBF file: {pbf_file} instead of overpass.")
        pbf_filepath = pbf_file
    else:
        # Reasonable subtask_percentages we're determined by profiling code sections on a developer workstation
        # TODO: Biggest impact to improving ETA estimates reqs higher fidelity tracking of run_query and convert

        # --- Overpass Query
        op = overpass.Overpass(
            bbox=bbox,
            stage_dir=stage_dir,
            slug=slug,
            url=url,
            job_name=job_name,
            task_uid=export_task_record_uid,
            raw_data_filename="{}_query.osm".format(job_name),
            config=config,
        )

        osm_data_filename = op.run_query(user_details=user_details, subtask_percentage=65, eta=eta)  # run the query

        # --- Convert Overpass result to PBF
        osm_filename = os.path.join(stage_dir, osm_data_filename)
        pbf_filename = os.path.join(stage_dir, "{}_query.pbf".format(job_name))
        pbf_filepath = pbf.OSMToPBF(osm=osm_filename, pbffile=pbf_filename, task_uid=export_task_record_uid).convert()

    # --- Generate thematic gpkg from PBF
    provider_slug = get_export_task_record(export_task_record_uid).export_provider_task.provider.slug
    gpkg_filepath = get_export_filepath(stage_dir, job_name, projection, provider_slug, "gpkg")

    feature_selection = FeatureSelection.example(clean_config(config))

    update_progress(export_task_record_uid, progress=67, eta=eta, msg="Converting data to Geopackage")
    geom = get_geometry(bbox, selection)

    g = geopackage.Geopackage(
        pbf_filepath, gpkg_filepath, stage_dir, feature_selection, geom, export_task_record_uid=export_task_record_uid
    )

    osm_gpkg = g.run(subtask_start=77, subtask_percentage=8, eta=eta)  # 77% to 85%
    if not osm_gpkg:
        export_task_record = get_export_task_record(export_task_record_uid)
        cancel_export_provider_task.run(
            data_provider_task_uid=export_task_record.export_provider_task.uid,
            message="No OSM data was returned for the selected area.",
        )

    # --- Add the Land Boundaries polygon layer, this accounts for the majority of post-processing time
    update_progress(export_task_record_uid, 85.5, eta=eta, msg="Clipping data in Geopackage")

    database = settings.DATABASES["feature_data"]
    in_dataset = "PG:dbname={name} host={host} user={user} password={password} port={port}".format(
        host=database["HOST"],
        user=database["USER"],
        password=database["PASSWORD"].replace("$", "\$"),
        port=database["PORT"],
        name=database["NAME"],
    )
    gdalutils.convert(
        boundary=selection,
        input_file=in_dataset,
        output_file=gpkg_filepath,
        layers=["land_polygons"],
        driver="gpkg",
        is_raster=False,
        access_mode="append",
        layer_creation_options=["GEOMETRY_NAME=geom"],  # Needed for current styles (see note below).
    )

    # TODO:  The arcgis templates as of version 1.9.0 rely on both OGC_FID and FID field existing.
    #  Just add the fid field if missing for now.
    with sqlite3.connect(gpkg_filepath) as conn:
        for column in ["fid", "ogc_fid"]:
            other_column = "ogc_fid" if column == "fid" else "fid"
            try:
                conn.execute(f"ALTER TABLE land_polygons ADD COLUMN {column} INTEGER NOT NULL DEFAULT (0);")
                conn.execute(f"UPDATE TABLE land_polygons SET {column} = {other_column};")
            except Exception as e:
                logger.error(e)
                # Column exists move on.
                pass

    ret_gpkg_filepath = g.results[0].parts[0]
    assert ret_gpkg_filepath == gpkg_filepath
    update_progress(
        export_task_record_uid, progress=100, eta=eta, msg="Completed OSM data collection pipeline",
    )
    result = {"pbf": pbf_filepath, "gpkg": gpkg_filepath}

    return result


@app.task(name="Create OSM", bind=True, base=ExportTask, abort_on_error=True, acks_late=True)
def osm_data_collection_task(
    self,
    result=None,
    stage_dir=None,
    run_uid=None,
    provider_slug=None,
    overpass_url=None,
    task_uid=None,
    job_name="no_job_name_specified",
    bbox=None,
    user_details=None,
    config=None,
    *args,
    **kwargs,
):
    """
    Collects data from OSM & produces a thematic gpkg as a subtask of the task referenced by export_provider_task_id.
    bbox expected format is an iterable of the form [ long0, lat0, long1, lat1 ]
    """
    logger.debug("enter run for {0}".format(self.name))
    debug_os = None

    try:
        # Uncomment debug_os to generate a simple CSV of the progress log that can be used to evaluate the accuracy
        # of ETA estimates
        # debug_os = open("{}_progress_log.csv".format(task_uid), 'w')
        eta = ETA(task_uid=task_uid, debug_os=debug_os)

        result = result or {}

        if user_details is None:
            user_details = {"username": "username not set in osm_data_collection_task"}

        selection = parse_result(result, "selection")
        osm_results = osm_data_collection_pipeline(
            task_uid,
            stage_dir,
            slug=provider_slug,
            job_name=job_name,
            bbox=bbox,
            user_details=user_details,
            selection=selection,
            url=overpass_url,
            config=config,
            eta=eta,
        )

        result.update(osm_results)
        result["result"] = osm_results.get("gpkg")
        result["source"] = osm_results.get("gpkg")

        logger.debug("exit run for {0}".format(self.name))
    finally:
        if debug_os:
            debug_os.close()

    return result


def add_metadata(job, provider, retval):
    """
    Accepts a job, provider slug, and return value from a task and applies metadata to the relevant file.

    :param job:
    :param provider_slug:
    :param retval:
    :return:
    """
    result_file = retval.get("result", None)
    if result_file is None:
        return
    task = metadata_tasks.get(os.path.splitext(result_file)[1], None)
    if not provider:
        return
    if task is not None:
        task(filepath=result_file, job=job, provider=provider)


@app.task(name="ESRI Shapefile (.shp)", bind=True, base=FormatTask, acks_late=True)
def shp_export_task(
    self,
    result=None,
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    user_details=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining SHP export function.
    """
    result = result or {}
    shp_in_dataset = parse_result(result, "source")

    provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug
    shp_out_dataset = get_export_filepath(stage_dir, job_name, projection, provider_slug, "shp")
    selection = parse_result(result, "selection")

    shp = gdalutils.convert(
        driver="ESRI Shapefile",
        input_file=shp_in_dataset,
        output_file=shp_out_dataset,
        task_uid=task_uid,
        boundary=selection,
        projection=projection,
    )

    result["driver"] = "ESRI Shapefile"
    result["result"] = shp
    return result


@app.task(name="Keyhole Markup Language (.kml)", bind=True, base=FormatTask, acks_late=True)
def kml_export_task(
    self,
    result=None,
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    config=None,
    user_details=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining KML export function.
    """
    result = result or {}

    provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug
    kml_out_dataset = get_export_filepath(stage_dir, job_name, projection, provider_slug, "kml")

    dptr = DataProviderTaskRecord.objects.get(tasks__uid__exact=task_uid)
    metadata = get_metadata(data_provider_task_record_uids=[dptr.uid], source_only=True)
    metadata["projections"] = [4326]

    try:
        import qgis  # noqa

        qgs_file = generate_qgs_style(metadata)
        kml = convert_qgis_gpkg_to_kml(qgs_file, kml_out_dataset, stage_dir=stage_dir)
    except ImportError:
        logger.info("QGIS is not installed, using gdalutils.convert.")
        kml_in_dataset = parse_result(result, "source")
        selection = parse_result(result, "selection")
        kml = gdalutils.convert(
            driver="libkml",
            input_file=kml_in_dataset,
            output_file=kml_out_dataset,
            task_uid=task_uid,
            boundary=selection,
            projection=projection,
        )

    result["driver"] = "libkml"
    result["file_extension"] = "kml"
    result["result"] = kml
    return result


@app.task(name="GPS Exchange (.gpx)", bind=True, base=FormatTask, acks_late=True)
def gpx_export_task(
    self,
    result=None,
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    config=None,
    user_details=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining GPX export function.
    """
    result = result or {}
    # Need to use PBF instead of GPKG because gpkg uses multi-geometry types whereas gpx doesn't support multipoint
    input_file = parse_result(result, "pbf")
    if not input_file:
        input_file = parse_result(result, "gpkg")
    # Need to crop to selection since the PBF hasn't been clipped.
    selection = parse_result(result, "selection")
    provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug
    gpx_file = get_export_filepath(stage_dir, job_name, projection, provider_slug, "gpx")
    try:
        out = gdalutils.convert(
            input_file=input_file,
            output_file=gpx_file,
            driver="GPX",
            dataset_creation_options=["GPX_USE_EXTENSIONS=YES"],
            creation_options=["-explodecollections"],
            boundary=selection,
        )
        result["file_extension"] = "gpx"
        result["driver"] = "GPX"
        result["result"] = out
        result["gpx"] = out
        return result
    except Exception as e:
        logger.error("Raised exception in gpx export, %s", str(e))
        raise Exception(e)


@app.task(name="OSM PBF (.pbf)", bind=True, base=FormatTask, acks_late=True)
def pbf_export_task(
    self, result=None, *args, **kwargs,
):
    """
    Function defining PBF export function, this format is already generated in the OSM step.  It just needs to be
    exposed and passed through.
    """
    result = result or {}
    logger.error("GETTING PBF FILE...")
    pbf_file = parse_result(result, "pbf")
    logger.error(pbf_file)
    try:
        result["file_extension"] = "pbf"
        result["driver"] = "OSM"
        result["result"] = pbf_file
        logger.error(f"Returning PBF RESULT: {result}")
        return result
    except Exception as e:
        logger.error("Raised exception in pbf export, %s", str(e))
        raise Exception(e)


@app.task(name="OGC API Process", bind=True, base=ExportTask, abort_on_error=True)
def ogcapi_process_export_task(
    self,
    result=None,
    config=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    bbox=None,
    service_url=None,
    projection=4326,
    session_token=None,
    export_format_slug=None,
    *args,
    **kwargs,
):
    """
    Function defining OGC API Processes export.
    """

    result = result or {}
    selection = parse_result(result, "selection")
    export_task_record = get_export_task_record(task_uid)
    data_provider = export_task_record.export_provider_task.provider
    if export_task_record.export_provider_task.provider.data_type == GeospatialDataType.ELEVATION.value:
        output_file = get_export_filepath(stage_dir, job_name, projection, data_provider.slug, "tif")
        driver = "gtiff"
    else:
        output_file = get_export_filepath(stage_dir, job_name, projection, data_provider.slug, "gpkg")
        driver = "gpkg"
    ogc_config = clean_config(config, return_dict=True).get("ogcapi_process", dict())
    download_path = get_export_filepath(stage_dir, f"{job_name}-source", projection, data_provider.slug, "zip")

    # TODO: The download path might not be a zip, use the mediatype to determine the file format.
    download_path = get_ogcapi_data(
        config=config,
        task_uid=task_uid,
        stage_dir=stage_dir,
        bbox=bbox,
        service_url=service_url,
        session_token=session_token,
        export_format_slug=export_format_slug,
        selection=selection,
        download_path=download_path,
    )

    if not export_format_slug:
        # TODO: Its possible the data is not in a zip, this step should be optional depending on output.
        source_data = find_in_zip(download_path, ogc_config.get("output_file_ext"), stage_dir)
        out = gdalutils.convert(
            driver=driver,
            input_file=source_data,
            output_file=output_file,
            task_uid=task_uid,
            projection=projection,
            boundary=bbox,
        )

        result["driver"] = driver
        result["file_extension"] = ogc_config.get("output_file_ext")
        result["ogcapi_process"] = download_path
        result["source"] = out
        result[driver] = out

    result["result"] = download_path
    logger.error(f"OGC PROCESS RESULT: {result}")
    return result


@app.task(name="OGC API Process Data", bind=True, base=FormatTask, acks_late=True)
def ogc_result_task(
    self,
    result,
    task_uid=None,
    export_format_slug=None,
    stage_dir=None,
    job_name=None,
    projection=None,
    bbox=None,
    service_url=None,
    session_token=None,
    *args,
    **kwargs,
):
    """
    Function defining PBF export function, this format is already generated in the OSM step.  It just needs to be
    exposed and passed through.
    """

    result = result or {}

    export_task_record = get_export_task_record(task_uid)
    selection = parse_result(result, "selection")
    data_provider: DataProvider = export_task_record.export_provider_task.provider
    export_format = ExportFormat.objects.get(slug=export_format_slug)
    ogcapi_config = load_provider_config(data_provider.config).get("ogcapi_process")
    if ogcapi_config:
        format_field = get_format_field_from_config(ogcapi_config)
        if format_field:
            if ogcapi_config["inputs"][format_field]["value"] == export_format_slug:
                logger.error(f"OGC DATA RESULT: {result}")
                result["result"] = result["ogcapi_process"]
                return result
    download_path = get_export_filepath(
        stage_dir, f"{job_name}-{normalize_name(export_format.name)}", projection, data_provider.slug, "zip"
    )
    download_path = get_ogcapi_data(
        config=data_provider.config,
        task_uid=task_uid,
        stage_dir=stage_dir,
        bbox=bbox,
        service_url=service_url,
        session_token=session_token,
        export_format_slug=export_format_slug,
        selection=selection,
        download_path=download_path,
    )

    result["result"] = download_path
    logger.error(f"OGC DATA RESULT: {result}")

    return result


@app.task(name="SQLITE Format", bind=True, base=FormatTask, acks_late=True)
def sqlite_export_task(
    self,
    result=None,
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    config=None,
    user_details=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining SQLITE export function.
    """
    result = result or {}
    sqlite_in_dataset = parse_result(result, "source")

    provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug
    sqlite_out_dataset = get_export_filepath(stage_dir, job_name, projection, provider_slug, "sqlite")
    selection = parse_result(result, "selection")

    sqlite = gdalutils.convert(
        driver="SQLite",
        input_file=sqlite_in_dataset,
        output_file=sqlite_out_dataset,
        task_uid=task_uid,
        boundary=selection,
        projection=projection,
    )

    result["driver"] = "SQLite"
    result["result"] = sqlite
    return result


@app.task(name="Create Area of Interest", bind=True, base=ExportTask, acks_late=True)
def output_selection_geojson_task(
    self,
    result=None,
    task_uid=None,
    selection=None,
    stage_dir=None,
    provider_slug=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining geopackage export function.
    """
    result = result or {}

    geojson_file = os.path.join(stage_dir, "{0}-{1}_selection.geojson".format(provider_slug, projection))

    if selection:
        # Test if json.
        json.loads(selection)

        from audit_logging.file_logging import logging_open

        user_details = kwargs.get("user_details")
        with logging_open(geojson_file, "w", user_details=user_details) as open_file:
            open_file.write(selection)
        result["selection"] = geojson_file
        result["result"] = geojson_file

    return result


@app.task(name="Geopackage (.gpkg)", bind=True, base=FormatTask, acks_late=True)
def geopackage_export_task(
    self,
    result=None,
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    user_details=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining geopackage export function.
    """
    result = result or {}
    gpkg = parse_result(result, "gpkg")
    if not gpkg:
        gpkg_in_dataset = parse_result(result, "source")

        provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug
        gpkg_out_dataset = get_export_filepath(stage_dir, job_name, projection, provider_slug, "gpkg")
        selection = parse_result(result, "selection")

        # This assumes that the source dataset has already been "clipped".  Since most things are tiles or selected
        # based on area it doesn't make sense to run this again.  If that isn't true this may need to be updated.
        if os.path.splitext(gpkg_in_dataset)[1] == ".gpkg":
            os.rename(gpkg_in_dataset, gpkg_out_dataset)
            gpkg = gpkg_out_dataset
        else:
            gpkg = gdalutils.convert(
                driver="gpkg",
                input_file=gpkg_in_dataset,
                output_file=gpkg_out_dataset,
                task_uid=task_uid,
                boundary=selection,
                projection=projection,
            )

    result["driver"] = "gpkg"
    result["result"] = gpkg
    result["gpkg"] = gpkg
    return result


@app.task(name="MBtiles (.mbtiles)", bind=True, base=FormatTask, acks_late=True)
def mbtiles_export_task(
    self,
    result=None,
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    user_details=None,
    projection=3857,  # MBTiles only support 3857
    *args,
    **kwargs,
):
    """
    Function defining mbtiles export function.
    """

    if projection != 3857:
        raise Exception("MBTiles only supports 3857.")
    result = result or {}
    provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug

    source_dataset = parse_result(result, "source")

    mbtiles_out_dataset = get_export_filepath(stage_dir, job_name, projection, provider_slug, "mbtiles")
    selection = parse_result(result, "selection")
    logger.error(f"Converting {source_dataset} to {mbtiles_out_dataset}")

    mbtiles = gdalutils.convert(
        driver="MBTiles",
        src_srs=4326,
        input_file=source_dataset,
        output_file=mbtiles_out_dataset,
        task_uid=task_uid,
        boundary=selection,
        projection=projection,
        use_translate=True,
    )

    result["driver"] = "MBTiles"
    result["result"] = mbtiles
    return result


@app.task(name="Geotiff (.tif)", bind=True, base=FormatTask, acks_late=True)
def geotiff_export_task(
    self, result=None, task_uid=None, stage_dir=None, job_name=None, projection=4326, config=None, *args, **kwargs
):
    """
    Function defining geopackage export function.
    """
    result = result or {}
    gtiff_out_dataset = parse_result(result, "gtiff")
    if not gtiff_out_dataset:
        gtiff_in_dataset = parse_result(result, "source")
        provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug
        gtiff_out_dataset = get_export_filepath(stage_dir, job_name, projection, provider_slug, "tif")
        selection = parse_result(result, "selection")

        warp_params, translate_params = get_creation_options(config, "gtiff")

        if "tif" in os.path.splitext(gtiff_in_dataset)[1]:
            gtiff_in_dataset = f"GTIFF_RAW:{gtiff_in_dataset}"

        gtiff_out_dataset = gdalutils.convert(
            driver="gtiff",
            input_file=gtiff_in_dataset,
            output_file=gtiff_out_dataset,
            task_uid=task_uid,
            boundary=selection,
            warp_params=warp_params,
            translate_params=translate_params,
        )

    result["file_extension"] = "tif"
    result["driver"] = "gtiff"
    result["result"] = gtiff_out_dataset
    result["gtiff"] = gtiff_out_dataset

    return result


@app.task(name="National Imagery Transmission Format (.nitf)", bind=True, base=FormatTask, acks_late=True)
def nitf_export_task(
    self,
    result=None,
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    user_details=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining nitf export function.
    """
    result = result or {}

    nitf_in_dataset = parse_result(result, "source")
    provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug
    nitf_out_dataset = get_export_filepath(stage_dir, job_name, projection, provider_slug, "nitf")

    creation_options = ["ICORDS=G"]
    nitf = gdalutils.convert(
        driver="nitf",
        input_file=nitf_in_dataset,
        output_file=nitf_out_dataset,
        task_uid=task_uid,
        creation_options=creation_options,
    )

    result["driver"] = "nitf"
    result["result"] = nitf
    result["nitf"] = nitf
    return result


@app.task(name="Erdas Imagine HFA (.img)", bind=True, base=FormatTask, acks_late=True)
def hfa_export_task(
    self,
    result=None,
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    config=None,
    user_details=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining Erdas Imagine HFA (.img) export function.
    """
    result = result or {}
    hfa_in_dataset = parse_result(result, "source")
    provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug
    hfa_out_dataset = get_export_filepath(stage_dir, job_name, projection, provider_slug, "img")
    hfa = gdalutils.convert(driver="hfa", input_file=hfa_in_dataset, output_file=hfa_out_dataset, task_uid=task_uid)

    result["file_extension"] = "img"
    result["driver"] = "hfa"
    result["result"] = hfa
    result["hfa"] = hfa
    return result


@app.task(name="Reprojection Task", bind=True, base=FormatTask, acks_late=True)
def reprojection_task(
    self,
    result=None,
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    user_details=None,
    projection=None,
    config=None,
    *args,
    **kwargs,
):
    """
    Function defining a task that will reproject all file formats to the chosen projections.
    """
    result = result or {}
    driver = parse_result(result, "driver")
    selection = parse_result(result, "selection")

    if parse_result(result, "file_extension"):
        file_extension = parse_result(result, "file_extension")
    else:
        file_extension = driver

    in_dataset = parse_result(result, "source")
    provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug
    out_dataset = get_export_filepath(stage_dir, job_name, projection, provider_slug, file_extension)

    warp_params, translate_params = get_creation_options(config, driver)

    # This logic is only valid IFF this method only allows 4326 which is True as of 1.9.0.
    # This needs to be updated to compare the input and output if over source projections are allowed.
    if not projection or "4326" in str(projection):
        logger.info(f"Skipping projection and renaming {in_dataset} to {out_dataset}")
        os.rename(in_dataset, out_dataset)
        reprojection = out_dataset
    else:
        # If you are updating this see the note above about source projection.
        dptr: DataProviderTaskRecord = DataProviderTaskRecord.objects.select_related("run__job").get(
            tasks__uid__exact=task_uid
        )
        metadata = get_metadata(data_provider_task_record_uids=[dptr.uid], source_only=True)
        data_type = metadata["data_sources"][provider_slug].get("type")

        if "tif" in os.path.splitext(in_dataset)[1]:
            in_dataset = f"GTIFF_RAW:{in_dataset}"

        if (
            "gpkg" in os.path.splitext(in_dataset)[1]
            and driver == "gpkg"
            and data_type == GeospatialDataType.RASTER.value
        ):
            # Use MapProxy instead of GDAL so all the pyramids/zoom levels of the source are preserved.

            level_from = metadata["data_sources"][provider_slug].get("level_from")
            level_to = metadata["data_sources"][provider_slug].get("level_to")

            job_geom = dptr.run.job.the_geom
            job_geom.transform(projection)
            bbox = job_geom.extent
            mp = mapproxy.MapproxyGeopackage(
                gpkgfile=out_dataset,
                service_url=out_dataset,
                name=job_name,
                config=config,
                bbox=bbox,
                level_from=level_from,
                level_to=level_to,
                task_uid=task_uid,
                selection=selection,
                projection=projection,
                input_gpkg=in_dataset,
            )
            reprojection = mp.convert()

        else:
            reprojection = gdalutils.convert(
                driver=driver,
                input_file=in_dataset,
                output_file=out_dataset,
                task_uid=task_uid,
                projection=projection,
                boundary=selection,
                warp_params=warp_params,
                translate_params=translate_params,
            )

    result["result"] = reprojection

    return result


@app.task(name="Create WFS Export", bind=True, base=ExportTask, abort_on_error=True, acks_late=True)
def wfs_export_task(
    self,
    result=None,
    layer=None,
    config=str(),
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    bbox=None,
    service_url=None,
    name=None,
    service_type=None,
    user_details=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining geopackage export for WFS service.
    """
    result = result or {}
    export_task_record = get_export_task_record(task_uid)
    provider_slug = export_task_record.export_provider_task.provider.slug

    gpkg = get_export_filepath(stage_dir, job_name, projection, provider_slug, "gpkg")

    configuration = load_provider_config(config)

    vector_layer_data = configuration.get("vector_layers", [])
    if len(vector_layer_data):
        layers = {}

        for layer in vector_layer_data:
            path = get_export_filepath(stage_dir, job_name, f"{layer.get('name')}-{projection}", provider_slug, "gpkg")
            url = get_wfs_query_url(name, layer.get("url"), layer.get("name"), projection)
            layers[layer["name"]] = {
                "task_uid": task_uid,
                "url": url,
                "path": path,
                "base_path": os.path.join(stage_dir, f"{layer.get('name')}-{projection}"),
                "bbox": bbox,
                "cert_info": configuration.get("cert_info"),
                "layer_name": layer["name"],
                "projection": projection,
            }

        download_concurrently(layers.values(), configuration.get("concurrency"))

        for layer_name, layer in layers.items():
            out = gdalutils.convert(
                driver="gpkg",
                input_file=layer.get("path"),
                output_file=gpkg,
                task_uid=task_uid,
                projection=projection,
                boundary=bbox,
                layer_name=layer_name,
                access_mode="append",
            )

    else:
        out = merge_chunks(
            gpkg,
            export_task_record.export_provider_task.provider.layers[0],
            projection,
            task_uid,
            bbox,
            stage_dir,
            get_wfs_query_url(name, service_url, layer, projection),
            configuration.get("cert_info"),
        )

    result["driver"] = "gpkg"
    result["result"] = out
    result["source"] = out
    result["gpkg"] = out

    # Check for geopackage contents; gdal wfs driver fails silently
    if not geopackage.check_content_exists(out):
        logger.warning("Empty response: Unknown layer name '{}' or invalid AOI bounds".format(layer))

    return result


def get_wfs_query_url(
    name: str, service_url: str = None, layer: str = None, projection: int = None, bbox: list = None
) -> str:
    """
    Function generates WFS query URL
    """

    # Strip out query string parameters that might conflict
    service_url = re.sub(r"(?i)(?<=[?&])(version|service|request|typename|srsname)=.*?(&|$)", "", service_url)

    query_params = {
        "SERVICE": "WFS",
        "VERSION": "1.0.0",
        "REQUEST": "GetFeature",
        "TYPENAME": layer,
        "SRSNAME": f"EPSG:{projection}",
        "BBOX": str(bbox).strip("[]"),
    }
    if bbox is None:
        query_params["BBOX"] = "BBOX_PLACEHOLDER"

    query_str = urlencode(query_params, safe=":")

    if "?" in service_url:
        if "&" != service_url[-1]:
            service_url += "&"
        service_url += query_str
    else:
        service_url += "?" + query_str

    url = service_url
    cred = auth_requests.get_cred(cred_var=name, url=url)

    if cred:
        user, pw = cred
        if not re.search(r"(?<=://)[a-zA-Z0-9\-._~]+:[a-zA-Z0-9\-._~]+(?=@)", url):
            url = re.sub(r"(?<=://)", "%s:%s@" % (user, pw), url)

    return url


@app.task(name="Create WCS Export", bind=True, base=ExportTask, abort_on_error=True, acks_late=True)
def wcs_export_task(
    self,
    result=None,
    layer=None,
    config=None,
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    bbox=None,
    service_url=None,
    name=None,
    service_type=None,
    user_details=None,
    projection=4326,
    selection=None,
    *args,
    **kwargs,
):
    """
    Function defining export for WCS services
    """
    result = result or {}

    provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug
    out = get_export_filepath(stage_dir, job_name, projection, provider_slug, "tif")

    eta = ETA(task_uid=task_uid)
    task = ExportTaskRecord.objects.get(uid=task_uid)

    try:
        wcs_conv = wcs.WCSConverter(
            config=config,
            out=out,
            bbox=bbox,
            service_url=service_url,
            layer=layer,
            debug=True,
            name=name,
            task_uid=task_uid,
            fmt="gtiff",
            slug=task.export_provider_task.provider.slug,
            user_details=user_details,
            eta=eta,
        )
        out = wcs_conv.convert()

        result["result"] = out
        result["source"] = out

        return result
    except Exception as e:
        logger.error("Raised exception in WCS service export: %s", str(e))
        raise Exception(e)


@app.task(name="Create ArcGIS FeatureService Export", bind=True, base=ExportTask, abort_on_error=True, acks_late=True)
def arcgis_feature_service_export_task(
    self,
    result=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    bbox=None,
    service_url=None,
    projection=4326,
    config=str(),
    *args,
    **kwargs,
):
    """
    Function defining sqlite export for ArcFeatureService service.
    """

    result = result or {}
    export_task_record = get_export_task_record(task_uid)
    provider_slug = export_task_record.export_provider_task.provider.slug

    gpkg = get_export_filepath(stage_dir, job_name, projection, provider_slug, "gpkg")

    if not os.path.exists(os.path.dirname(gpkg)):
        os.makedirs(os.path.dirname(gpkg), 6600)

    configuration = load_provider_config(config)

    vector_layer_data = configuration.get("vector_layers", [])
    out = None
    if len(vector_layer_data):
        layers = {}
        for layer in vector_layer_data:
            # TODO: using wrong signature for filepath, however pipeline counts on projection-provider_slug.ext.
            path = get_export_filepath(stage_dir, job_name, f"{layer.get('name')}-{projection}", provider_slug, "gpkg")
            url = get_arcgis_query_url(layer.get("url"))
            layers[layer["name"]] = {
                "task_uid": task_uid,
                "url": url,
                "path": path,
                "base_path": os.path.join(stage_dir, f"{layer.get('name')}-{projection}"),
                "bbox": bbox,
                "cert_info": configuration.get("cert_info"),
                "layer_name": layer.get("name"),
                "projection": projection,
                "distinct_field": layer.get("distinct_field"),
            }

        try:
            download_concurrently(layers.values(), configuration.get("concurrency"), feature_data=True)
        except Exception as e:
            logger.error(f"ArcGIS provider download error: {e}")
            raise e

        for layer_name, layer in layers.items():
            out = gdalutils.convert(
                driver="gpkg",
                input_file=layer.get("path"),
                output_file=gpkg,
                task_uid=task_uid,
                boundary=bbox,
                projection=projection,
                layer_name=layer_name,
                access_mode="append",
            )

    else:
        out = merge_chunks(
            gpkg,
            export_task_record.export_provider_task.provider.layers[0],
            projection,
            task_uid,
            bbox,
            stage_dir,
            get_arcgis_query_url(service_url),
            configuration.get("cert_info"),
            feature_data=True,
        )

    if not (out and geopackage.check_content_exists(out)):
        raise Exception("The service returned no data for the selected area.")

    result["driver"] = "gpkg"
    result["result"] = out
    result["source"] = out
    result["gpkg"] = out

    return result


def get_arcgis_query_url(service_url: str, bbox: list = None) -> str:
    """
    Function generates ArcGIS query URL
    """

    try:
        # remove any url query so we can add our own
        service_url = service_url.split("/query?")[0]
    except ValueError:
        # if no url query we can just check for trailing slash and move on
        service_url = service_url.rstrip("/\\")
    finally:
        query_params = {
            "where": "objectid=objectid",
            "outfields": "*",
            "f": "json",
            "geometry": str(bbox).strip("[]"),
        }
        if bbox is None:
            query_params["geometry"] = "BBOX_PLACEHOLDER"
        query_str = urlencode(query_params, safe="=*")
        query_url = urljoin(f"{service_url}/", f"query?{query_str}")

    return query_url


@app.task(name="Create Vector File Export", bind=True, base=ExportTask, abort_on_error=True)
def vector_file_export_task(
    self,
    result=None,
    layer=None,
    config=str(),
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    bbox=None,
    service_url=None,
    name=None,
    service_type=None,
    user_details=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining geopackage export for geospatial vector file service.
    """
    result = result or {}
    export_task_record = get_export_task_record(task_uid)
    provider_slug = export_task_record.export_provider_task.provider.slug

    gpkg = get_export_filepath(stage_dir, job_name, projection, provider_slug, "gpkg")

    configuration = load_provider_config(config)

    download_data(task_uid, service_url, gpkg, cert_info=configuration.get("cert_info"), provider_slug=provider_slug)

    out = gdalutils.convert(
        driver="gpkg",
        input_file=gpkg,
        output_file=gpkg,
        task_uid=task_uid,
        projection=projection,
        layer_name=export_task_record.export_provider_task.provider.layers[0],
        boundary=bbox,
        is_raster=False,
    )

    result["driver"] = "gpkg"
    result["result"] = out
    result["source"] = out
    result["gpkg"] = out

    return result


@app.task(name="Create Raster File Export", bind=True, base=ExportTask, abort_on_error=True)
def raster_file_export_task(
    self,
    result=None,
    layer=None,
    config=str(),
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    bbox=None,
    service_url=None,
    name=None,
    service_type=None,
    user_details=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining geopackage export for geospatial raster file service.
    """
    result = result or {}
    export_task_record = get_export_task_record(task_uid)
    provider_slug = export_task_record.export_provider_task.provider.slug

    gpkg = get_export_filepath(stage_dir, job_name, projection, provider_slug, "gpkg")

    configuration = load_provider_config(config)

    download_data(task_uid, service_url, gpkg, cert_info=configuration.get("cert_info"), provider_slug=provider_slug)

    out = gdalutils.convert(
        driver="gpkg",
        input_file=gpkg,
        output_file=gpkg,
        task_uid=task_uid,
        projection=projection,
        boundary=bbox,
        is_raster=True,
    )

    result["driver"] = "gpkg"
    result["result"] = out
    result["source"] = out
    result["gpkg"] = out

    return result


@app.task(name="Create Bounds Export", bind=True, base=ExportTask)
def bounds_export_task(
    self, result={}, run_uid=None, task_uid=None, stage_dir=None, provider_slug=None, projection=4326, *args, **kwargs
):
    """
    Function defining geopackage export function.
    """
    user_details = kwargs.get("user_details")
    # This is just to make it easier to trace when user_details haven't been sent
    if user_details is None:
        user_details = {"username": "unknown-bounds_export_task"}

    run = ExportRun.objects.get(uid=run_uid)

    result_gpkg = parse_result(result, "source")
    bounds = run.job.the_geom.geojson or run.job.bounds_geojson

    gpkg = os.path.join(stage_dir, "{0}-{1}_bounds.gpkg".format(provider_slug, projection))
    gpkg = geopackage.add_geojson_to_geopackage(
        geojson=bounds, gpkg=gpkg, layer_name="bounds", task_uid=task_uid, user_details=user_details
    )

    result["result"] = gpkg
    result["source"] = result_gpkg
    return result


@app.task(name="Create Raster Export", bind=True, base=ExportTask, abort_on_error=True, acks_late=True)
def mapproxy_export_task(
    self,
    result=None,
    layer=None,
    config=None,
    run_uid=None,
    task_uid=None,
    stage_dir=None,
    job_name=None,
    bbox=None,
    service_url=None,
    level_from=None,
    level_to=None,
    name=None,
    service_type=None,
    projection=4326,
    *args,
    **kwargs,
):
    """
    Function defining geopackage export for external raster service.
    """
    result = result or {}
    selection = parse_result(result, "selection")

    provider_slug = get_export_task_record(task_uid).export_provider_task.provider.slug
    gpkgfile = get_export_filepath(stage_dir, job_name, projection, provider_slug, "gpkg")

    try:
        w2g = mapproxy.MapproxyGeopackage(
            gpkgfile=gpkgfile,
            bbox=bbox,
            service_url=service_url,
            name=name,
            layer=layer,
            config=config,
            level_from=level_from,
            level_to=level_to,
            service_type=service_type,
            task_uid=task_uid,
            selection=selection,
        )
        gpkg = w2g.convert()
        result["driver"] = "gpkg"
        result["result"] = gpkg
        result["source"] = gpkg

        return result
    except Exception as e:
        logger.error(f"Raised exception in raster service export, {e}")
        raise e


@app.task(name="Pickup Run", bind=True, base=UserDetailsBase, acks_late=True)
def pick_up_run_task(
    self,
    result=None,
    run_uid=None,
    user_details=None,
    data_provider_slugs=None,
    run_zip_file_slug_sets=None,
    session_token=None,
    *args,
    **kwargs,
):
    """
    Generates a Celery task to assign a celery pipeline to a specific worker.
    """
    from eventkit_cloud.tasks.task_factory import TaskFactory

    # This is just to make it easier to trace when user_details haven't been sent
    worker = socket.gethostname()
    queue_group = get_celery_queue_group(run_uid=run_uid, worker=worker)

    if user_details is None:
        user_details = {"username": "unknown-pick_up_run_task"}
    run = ExportRun.objects.get(uid=run_uid)
    try:
        logger.debug(f"Worker for {run.uid} is {worker} using queue_group {queue_group}")
        data_provider_task_records = run.data_provider_task_records.filter(~Q(slug="run"))
        logger.debug(f"Current tasks for run: {[dptr.name for dptr in data_provider_task_records]}")
        if not data_provider_task_records:
            TaskFactory().parse_tasks(
                worker=worker,
                run_uid=run_uid,
                user_details=user_details,
                data_provider_slugs=data_provider_slugs,
                run_zip_file_slug_sets=run_zip_file_slug_sets,
                session_token=session_token,
                queue_group=queue_group,
            )
        else:
            # Run has already been created, but we got here because
            # something happened to the last worker and the run didn't finish.
            logger.info(f"Run {run.uid} was already in progress!")
            data_provider_slugs = [
                dptr.slug
                for dptr in run.data_provider_task_records.filter(~Q(slug="run") | ~Q(status=TaskState.COMPLETED.value))
            ]
            logger.info(f"Rerunning data providers {data_provider_slugs}")
            rerun_data_provider_records(run_uid, run.user.id, user_details, data_provider_slugs)
        run.worker = worker
        run.save()
    except Exception as e:
        run.status = TaskState.FAILED.value
        run.save()
        logger.error(str(e))
        raise


def wait_for_run(run_uid: str = None) -> None:
    """
    :param run_uid: The uid of the run to wait on.
    :return: None
    """
    run = ExportRun.objects.get(uid=run_uid)
    if run.status:
        while (
            TaskState[run.status] not in TaskState.get_finished_states()
            and TaskState[run.status] not in TaskState.get_incomplete_states()
        ):
            time.sleep(10)
            run.refresh_from_db()


# This could be improved by using Redis or Memcached to help manage state.
@app.task(name="Wait For Providers", base=EventKitBaseTask, acks_late=True)
def wait_for_providers_task(result=None, apply_args=None, run_uid=None, callback_task=None, *args, **kwargs):
    if isinstance(callback_task, dict):
        callback_task = signature(callback_task)

    run = ExportRun.objects.filter(uid=run_uid).first()
    if run:
        provider_tasks = run.data_provider_task_records.filter(~Q(slug="run"))
        if all(TaskState[provider_task.status] in TaskState.get_finished_states() for provider_task in provider_tasks):
            callback_task.apply_async(**apply_args)
        else:
            logger.warning(f"The run: {run_uid} is Waiting for other tasks to finish.")
    else:
        raise Exception("A run could not be found for uid {0}".format(run_uid))


@app.task(name="Project File (.zip)", base=ZipFileTask, acks_late=True)
def create_zip_task(
    result: dict = None,
    data_provider_task_record_uid: List[str] = None,
    data_provider_task_record_uids: List[str] = None,
    run_zip_file_uid=None,
    *args,
    **kwargs,
):
    """
    :param result: The celery task result value, it should be a dict with the current state.
    :param data_provider_task_record_uid: A data provider task record UID to zip.
    :param data_provider_task_record_uids: A list of data provider task record UIDs to zip.
    :param run_zip_file_uid: The UUID of the zip file.
    :return: The run files, or a single zip file if data_provider_task_record_uid is passed.
    """

    if not result:
        result = {}

    if not data_provider_task_record_uids:
        data_provider_task_record = DataProviderTaskRecord.objects.get(uid=data_provider_task_record_uid)
        data_provider_task_records = data_provider_task_record.run.data_provider_task_records.exclude(slug="run")
        data_provider_task_record_uids = [
            data_provider_task_record.uid for data_provider_task_record in data_provider_task_records
        ]

    if len(data_provider_task_record_uids) > 1:
        data_provider_task_record_slug = "run"
    elif len(data_provider_task_record_uids) == 1:
        data_provider_task_record_slug = (
            DataProviderTaskRecord.objects.select_related("provider")
            .get(uid=data_provider_task_record_uids[0])
            .provider.slug
        )

    metadata = get_metadata(data_provider_task_record_uids)
    include_files = metadata.get("include_files", None)

    if include_files:
        arcgis_dir = os.path.join(get_run_staging_dir(metadata["run_uid"]), Directory.ARCGIS.value)
        make_dirs(arcgis_dir)
        arcgis_metadata_file = os.path.join(arcgis_dir, "metadata.json")
        arcgis_metadata = get_arcgis_metadata(metadata)
        with open(arcgis_metadata_file, "w") as open_md_file:
            json.dump(arcgis_metadata, open_md_file)
        include_files += [arcgis_metadata_file]
        # No need to add QGIS file if there aren't any files to be zipped.
        include_files += [generate_qgs_style(metadata)]
        include_files += [get_human_readable_metadata_document(metadata)]

        ogc_metadata_dir = os.path.join(
            os.path.join(get_run_staging_dir(metadata["run_uid"]), data_provider_task_record_slug), "metadata"
        )

        # TODO: make sure files are placed in the '../metadata' directory.
        if os.path.isdir(ogc_metadata_dir):
            path = Path(ogc_metadata_dir)
            files = [str(file_) for file_ in path.rglob("*")]
            include_files.extend(files)

        # Need to remove duplicates from the list because
        # some intermediate tasks produce files with the same name.
        # and add the static resources
        include_files = list(set(include_files))

        zip_file_name = f"{metadata['name']}.zip"

        result["result"] = zip_files(
            include_files=include_files,
            run_zip_file_uid=run_zip_file_uid,
            file_path=os.path.join(
                get_provider_staging_dir(metadata["run_uid"], data_provider_task_record_slug), zip_file_name
            ),
            static_files=get_style_files(),
            metadata=metadata,
        )
    else:
        raise Exception("Could not create a zip file because there were not files to include.")
    return result


@app.task(name="Finalize Export Provider Task", base=EventKitBaseTask, acks_late=True)
def finalize_export_provider_task(result=None, data_provider_task_uid=None, status=None, *args, **kwargs):
    """
    Finalizes provider task.

    Cleans up staging directory.
    Updates export provider status.
    """

    # if the status was a success, we can assume all the ExportTasks succeeded. if not, we need to parse ExportTasks to
    # mark tasks not run yet as canceled.

    result_status = parse_result(result, "status")

    with transaction.atomic():
        data_provider_task_record = DataProviderTaskRecord.objects.prefetch_related("tasks").get(
            uid=data_provider_task_uid
        )
        has_failures = any(
            [
                export_task_record.status == TaskState.FAILED.value
                for export_task_record in data_provider_task_record.tasks.all()
            ]
        )
        if TaskState[result_status] == TaskState.CANCELED:
            # This makes the assumption that users can't cancel individual tasks.  Therefore if any of them failed then
            # it is likely that the rest of the tasks were force canceled since they depend on the task that failed.
            if has_failures:
                data_provider_task_record.status = TaskState.INCOMPLETE.value
            else:
                data_provider_task_record.status = TaskState.CANCELED.value
        else:
            if has_failures:
                data_provider_task_record.status = TaskState.INCOMPLETE.value
            else:
                data_provider_task_record.status = TaskState.COMPLETED.value
        data_provider_task_record.finished_at = timezone.now()
        data_provider_task_record.save()

    return result


@gdalutils.retry
def zip_files(include_files, run_zip_file_uid, file_path=None, static_files=None, metadata=None, *args, **kwargs):
    """
    Contains the organization for the files within the archive.
    :param include_files: A list of files to be included.
    :param run_zip_file_uid: The UUID of the zip file.
    :param file_path: The name for the archive.
    :param static_files: Files that are in the same location for every datapack (i.e. templates and metadata files).
    :param metadata: A dict of user requested file information.
    :return: The zipfile path.
    """

    if not include_files:
        logger.error("zip_file_task called with no include_files.")
        raise Exception("zip_file_task called with no include_files.")

    if not file_path:
        logger.error("zip_file_task called with no file path.")
        raise Exception("zip_file_task called with no file path.")

    run_zip_file = RunZipFile.objects.get(uid=run_zip_file_uid)

    files = [filename for filename in include_files if os.path.splitext(filename)[-1] not in BLACKLISTED_ZIP_EXTS]

    manifest_ignore_files = []
    logger.debug("Opening the zipfile.")
    with ZipFile(file_path, "a", compression=ZIP_DEFLATED, allowZip64=True) as zipfile:
        if static_files:
            for absolute_file_path, relative_file_path in static_files.items():
                if "__pycache__" in absolute_file_path:
                    continue
                filename = relative_file_path
                # Support files should go in the correct directory.  It might make sense to break these files up
                # by directory and then just put each directory in the correct location so that we don't have to
                # list all support files in the future.
                basename = os.path.basename(absolute_file_path)
                if basename == "__init__.py":
                    continue
                elif os.path.basename(os.path.dirname(absolute_file_path)) == Directory.ARCGIS.value:
                    if basename in ["create_mxd.py", "create_aprx.py", "ReadMe.txt"]:
                        filename = os.path.join(Directory.ARCGIS.value, "{0}".format(basename))
                    else:
                        # Put the support files in the correct directory.
                        filename = os.path.join(
                            Directory.ARCGIS.value, Directory.TEMPLATES.value, "{0}".format(basename)
                        )
                manifest_ignore_files.append(filename)
                zipfile.write(absolute_file_path, arcname=filename)
        for filepath in files:
            # This takes files from the absolute stage paths and puts them in the provider directories in the data dir.
            # (e.g. staging_root/run_uid/provider_slug/file_name.ext -> data/provider_slug/file_name.ext)
            name, ext = os.path.splitext(filepath)
            provider_slug, name = os.path.split(name)
            provider_slug = os.path.split(provider_slug)[1]

            if filepath.endswith((".qgs", "ReadMe.txt")):
                # put the style file in the root of the zip
                filename = "{0}{1}".format(name, ext)
                manifest_ignore_files.append(filename)
            elif filepath.endswith("metadata.json"):
                # put the metadata file in arcgis folder unless it becomes more useful.
                filename = os.path.join(Directory.ARCGIS.value, "{0}{1}".format(name, ext))
                manifest_ignore_files.append(filename)
            elif filepath.endswith(PREVIEW_TAIL):
                download_filename = get_download_filename("preview", ext)
                filename = get_archive_data_path(provider_slug, download_filename)
                manifest_ignore_files.append(filename)
            else:
                # Put the files into directories based on their provider_slug
                # prepend with `data`

                download_filename = get_download_filename(name, ext)
                filename = get_archive_data_path(provider_slug, download_filename)
            run_zip_file.message = f"Adding {filename} to zip archive."
            zipfile.write(filepath, arcname=filename)

        manifest_file = get_data_package_manifest(metadata=metadata, ignore_files=manifest_ignore_files)
        zipfile.write(manifest_file, arcname=os.path.join("MANIFEST", os.path.basename(manifest_file)))
        add_export_run_files_to_zip(zipfile, run_zip_file)

        if zipfile.testzip():
            raise Exception("The zipped file was corrupted.")

    return file_path


class FinalizeRunBase(EventKitBaseTask):
    name = "Finalize Export Run"

    def run(self, result=None, run_uid=None, stage_dir=None):
        """
         Finalizes export run.

        Cleans up staging directory.
        Updates run with finish time.
        Emails user notification.
        """
        result = result or {}

        run = ExportRun.objects.get(uid=run_uid)
        run.status = TaskState.COMPLETED.value
        notification_level = NotificationLevel.SUCCESS.value
        verb = NotificationVerb.RUN_COMPLETED.value
        provider_tasks = run.data_provider_task_records.all()

        # Complicated Celery chain from TaskFactory.parse_tasks() is incorrectly running pieces in parallel;
        #    this waits until all provider tasks have finished before continuing.
        if any(getattr(TaskState, task.status, None) == TaskState.PENDING for task in provider_tasks):
            finalize_run_task.retry(
                result=result, run_uid=run_uid, stage_dir=stage_dir, interval_start=4, interval_max=10
            )

        # mark run as incomplete if any tasks fail
        if any(getattr(TaskState, task.status, None) in TaskState.get_incomplete_states() for task in provider_tasks):
            run.status = TaskState.INCOMPLETE.value
            notification_level = NotificationLevel.WARNING.value
            verb = NotificationVerb.RUN_FAILED.value
        if all(getattr(TaskState, task.status, None) == TaskState.CANCELED for task in provider_tasks):
            run.status = TaskState.CANCELED.value
            notification_level = NotificationLevel.WARNING.value
            verb = NotificationVerb.RUN_CANCELED.value
        finished = timezone.now()
        run.finished_at = finished
        run.save()

        # sendnotification to user via django notifications
        sendnotification(run, run.job.user, verb, None, None, notification_level, run.status)

        # send notification email to user
        site_url = settings.SITE_URL.rstrip("/")
        url = "{0}/status/{1}".format(site_url, run.job.uid)
        addr = run.user.email
        if run.status == TaskState.CANCELED.value:
            subject = "Your Eventkit Data Pack was CANCELED."
        else:
            subject = "Your Eventkit Data Pack is ready."
        to = [addr]
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "Eventkit Team <eventkit.team@gmail.com>")
        ctx = {"url": url, "status": run.status, "job_name": run.job.name}

        text = get_template("email/email.txt").render(ctx)
        html = get_template("email/email.html").render(ctx)
        try:
            msg = EmailMultiAlternatives(subject, text, to=to, from_email=from_email)
            msg.attach_alternative(html, "text/html")
            msg.send()
        except Exception as e:
            logger.error("Encountered an error when sending status email: {}".format(e))

        result["stage_dir"] = stage_dir
        return result

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        super(FinalizeRunBase, self).after_return(status, retval, task_id, args, kwargs, einfo)
        stage_dir = None if retval is None else retval.get("stage_dir")
        try:
            if stage_dir and os.path.isdir(stage_dir):
                if not os.getenv("KEEP_STAGE", False):
                    shutil.rmtree(stage_dir)
            if getattr(settings, "CELERY_SCALE_BY_RUN"):
                queue_name = None if retval is None else retval.get("run_uid")
                if queue_name:
                    shutdown_celery_workers.s().apply_async(queue=queue_name, routing_key=queue_name)
        except IOError or OSError:
            logger.error("Error removing {0} during export finalize".format(stage_dir))


# There's a celery bug with callbacks that use bind=True.  If altering this task do not use Bind.
# @see: https://github.com/celery/celery/issues/3723
@app.task(name="Finalize Run Task", base=FinalizeRunBase)
def finalize_run_task(result=None, run_uid=None, stage_dir=None, apply_args=None, *args, **kwargs):
    """
     Finalizes export run.

    Cleans up staging directory.
    Updates run with finish time.
    Emails user notification.
    """
    result = result or {}

    run: ExportRun = ExportRun.objects.prefetch_related("data_provider_task_records").get(uid=run_uid)
    run.status = TaskState.COMPLETED.value
    verb = NotificationVerb.RUN_COMPLETED.value
    notification_level = NotificationLevel.SUCCESS.value
    data_provider_task_records = run.data_provider_task_records.all()
    # mark run as incomplete if any tasks fail
    if any(
        getattr(TaskState, task.status, None) in TaskState.get_incomplete_states()
        for task in data_provider_task_records
    ):
        run.status = TaskState.INCOMPLETE.value
        notification_level = NotificationLevel.WARNING.value
        verb = NotificationVerb.RUN_FAILED.value
    if all(getattr(TaskState, task.status, None) == TaskState.CANCELED for task in data_provider_task_records):
        run.status = TaskState.CANCELED.value
        verb = NotificationVerb.RUN_CANCELED.value
        notification_level = NotificationLevel.WARNING.value

    run.finished_at = timezone.now()
    run.save()

    # sendnotification to user via django notifications

    sendnotification(run, run.job.user, verb, None, None, notification_level, run.status)

    # send notification email to user
    site_url = settings.SITE_URL.rstrip("/")
    url = "{0}/status/{1}".format(site_url, run.job.uid)
    addr = run.user.email
    if run.status == TaskState.CANCELED.value:
        subject = "Your Eventkit Data Pack was CANCELED."
    else:
        subject = "Your Eventkit Data Pack is ready."
    to = [addr]
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL")
    ctx = {"url": url, "status": run.status, "job_name": run.job.name}

    text = get_template("email/email.txt").render(ctx)
    html = get_template("email/email.html").render(ctx)
    try:
        msg = EmailMultiAlternatives(subject, text, to=to, from_email=from_email)
        msg.attach_alternative(html, "text/html")
        msg.send()
    except Exception as e:
        logger.error("Encountered an error when sending status email: {}".format(e))

    result["run_uid"] = run_uid
    result["stage_dir"] = stage_dir
    return result


@app.task(name="Export Task Error Handler", bind=True, base=EventKitBaseTask)
def export_task_error_handler(self, result=None, run_uid=None, task_id=None, stage_dir=None, *args, **kwargs):
    """
    Handles un-recoverable errors in export tasks.
    """
    result = result or {}
    try:
        run = ExportRun.objects.get(uid=run_uid)
        try:
            if os.path.isdir(stage_dir):
                if not os.getenv("KEEP_STAGE", False):
                    shutil.rmtree(stage_dir)
        except IOError:
            logger.error("Error removing {0} during export finalize".format(stage_dir))

        site_url = settings.SITE_URL
        url = "{0}/status/{1}".format(site_url.rstrip("/"), run.job.uid)
        addr = run.user.email
        subject = "Your Eventkit Data Pack has a failure."
        # email user and administrator
        to = [addr, settings.TASK_ERROR_EMAIL]
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "Eventkit Team <eventkit.team@gmail.com>")
        ctx = {"url": url, "task_id": task_id, "job_name": run.job.name}
        text = get_template("email/error_email.txt").render(ctx)
        html = get_template("email/error_email.html").render(ctx)
        msg = EmailMultiAlternatives(subject, text, to=to, from_email=from_email)
        msg.attach_alternative(html, "text/html")
        msg.send()

        # Send failed DataPack notifications to specific channel(s) or user(s) if enabled.
        rocketchat_notifications = settings.ROCKETCHAT_NOTIFICATIONS
        if rocketchat_notifications:
            if "channels" not in rocketchat_notifications:
                logger.error("Rocket Chat configuration missing or malformed.")
            channels = rocketchat_notifications["channels"]
            message = f"@here: A DataPack has failed during processing. {ctx['url']}"

            client = RocketChat(**rocketchat_notifications)
            for channel in channels:
                client.post_message(channel, message)
    except Exception as e:
        logger.exception(e)
    return result


def fail_synchronous_task_chain(data_provider_task_record=None):
    for export_task in data_provider_task_record.tasks.all():
        if TaskState[export_task.status] == TaskState.PENDING:
            export_task.status = TaskState.CANCELED.value
            export_task.save()
            kill_task.apply_async(
                kwargs={"task_pid": export_task.pid, "celery_uid": export_task.celery_uid},
                queue="{0}.priority".format(export_task.worker),
                priority=TaskPriority.CANCEL.value,
                routing_key="{0}.priority".format(export_task.worker),
            )


@app.task(name="Create preview", base=EventKitBaseTask, acks_late=True, reject_on_worker_lost=True)
def create_datapack_preview(
    result=None, run_uid=None, task_uid=None, stage_dir=None, task_record_uid=None, *args, **kwargs
):
    """
    Attempts to add a MapImageSnapshot (Preview Image) to a provider task.
    """
    result = result or {}
    try:
        from eventkit_cloud.utils.image_snapshot import get_wmts_snapshot_image, make_snapshot_downloadable, fit_to_area

        check_cached_task_failures(create_datapack_preview.name, task_uid)

        provider_task = DataProviderTask.objects.select_related("provider").get(uid=task_uid)
        provider = provider_task.provider

        provider_task_record = DataProviderTaskRecord.objects.get(uid=task_record_uid)

        export_run = ExportRun.objects.get(uid=run_uid)
        job = export_run.job

        filepath = get_provider_staging_preview(export_run.uid, provider.slug)
        make_dirs(stage_dir)
        preview = get_wmts_snapshot_image(provider.preview_url, bbox=job.extents)
        fit_to_area(preview)
        preview.save(filepath)

        download_filename = f"{run_uid}/{provider.slug}/{PREVIEW_TAIL}"
        snapshot = make_snapshot_downloadable(filepath, download_filename=download_filename, copy=True)
        provider_task_record.preview = snapshot
        provider_task_record.save()
        result["result"] = filepath

    except Exception as e:
        logger.exception(e)
    return result


@app.task(name="Cancel Export Provider Task", base=EventKitBaseTask)
def cancel_export_provider_task(
    result=None,
    data_provider_task_uid=None,
    canceling_username=None,
    delete=False,
    error=False,
    message=None,
    *args,
    **kwargs,
):
    """
    Cancels an DataProviderTaskRecord and terminates each subtasks execution.
    Checks if all DataProviderTasks for the Run grouping them have finished & updates the Run's status.
    """

    # There is enough over use of this class (i.e. for errors, deletions, canceling) the reason is because it had all
    # the working logic for stopping future jobs, but that can probably be abstracted a bit, and then let the caller
    # manage the task state (i.e. the task should be FAILED or CANCELED).
    result = result or {}
    data_provider_task_record = DataProviderTaskRecord.objects.get(uid=data_provider_task_uid)

    # There might not be a canceling user...
    try:
        canceling_user = User.objects.get(username=canceling_username)
    except ObjectDoesNotExist:
        canceling_user = None

    export_tasks = data_provider_task_record.tasks.all()

    # Loop through both the tasks in the DataProviderTaskRecord model, as well as the Task Chain in celery
    for export_task in export_tasks.all():
        if delete:
            exception_class = DeleteException
        else:
            exception_class = CancelException
        if TaskState[export_task.status] not in TaskState.get_finished_states():
            export_task.status = TaskState.CANCELED.value
            if canceling_user:
                export_task.cancel_user = canceling_user
            export_task.save()
        # This part is to populate the UI with the cancel message.  If a different mechanism is incorporated
        # to pass task information to the users, then it may make sense to replace this.
        try:
            raise exception_class(message=message, task_name=data_provider_task_record.name, user_name=canceling_user)
        except exception_class as ce:
            einfo = ExceptionInfo()
            einfo.exception = ce
            ExportTaskException.objects.create(task=export_task, exception=pickle_exception(einfo))

        # Remove the ExportTaskResult, which will clean up the files.
        task_result = export_task.result
        if task_result:
            task_result.soft_delete()

        if int(export_task.pid) > 0 and export_task.worker:
            kill_task.apply_async(
                kwargs={"task_pid": export_task.pid, "celery_uid": export_task.celery_uid},
                queue="{0}.priority".format(export_task.worker),
                priority=TaskPriority.CANCEL.value,
                routing_key="{0}.priority".format(export_task.worker),
            )

        # Add canceled to the cache so processes can check in to see if they should abort.
        set_cache_value(
            uid=export_task.uid, attribute="status", model_name="ExportTaskRecord", value=TaskState.CANCELED.value
        )

    if TaskState[data_provider_task_record.status] not in TaskState.get_finished_states():
        if error:
            data_provider_task_record.status = TaskState.FAILED.value
        else:
            data_provider_task_record.status = TaskState.CANCELED.value
    data_provider_task_record.save()

    return result


@app.task(name="Cancel Run", base=EventKitBaseTask)
def cancel_run(result=None, export_run_uid=None, canceling_username=None, delete=False, *args, **kwargs):
    result = result or {}

    export_run = ExportRun.objects.get(uid=export_run_uid)

    for export_provider_task in export_run.data_provider_task_records.all():
        cancel_export_provider_task(
            data_provider_task_uid=export_provider_task.uid,
            canceling_username=canceling_username,
            delete=delete,
            locking_task_key="cancel_export_provider_task-{0}".format(export_provider_task.uid),
        )
    result["result"] = True
    return result


@app.task(name="Kill Task", base=EventKitBaseTask)
def kill_task(result=None, task_pid=None, celery_uid=None, *args, **kwargs):
    """
    Asks a worker to kill a task.
    """
    import celery

    result = result or {}

    if celery_uid:
        try:
            # Ensure the task is still running otherwise the wrong process will be killed
            if AsyncResult(str(celery_uid), app=app).state == celery.states.STARTED:
                # If the task finished prior to receiving this kill message it could throw an OSError.
                logger.info("Attempting to kill {0}".format(task_pid))
                # Don't kill tasks with default pid.
                if int(task_pid) > 0:
                    progressive_kill(task_pid)
            else:
                logger.info(
                    "The celery_uid {0} has the status of {1}.".format(
                        celery_uid, AsyncResult(celery_uid, app=app).state
                    )
                )
        except OSError:
            logger.info("{0} PID does not exist.".format(task_pid))
    return result


def parse_result(task_result, key=""):
    """
    Used to parse the celery result for a specific value.
    :param task_result: A str, dict, or list.
    :param key: A key to search dicts for
    :return: The value at the desired key or the original task result if not in a list or dict.
    """
    if not task_result:
        return None
    if isinstance(task_result, list):
        for result in task_result:
            if result.get(key):
                return result.get(key)
    elif isinstance(task_result, dict):
        return task_result.get(key)


def get_function(function):
    from importlib import import_module

    module, function_name = function.rsplit(".", 1)

    module_object = import_module(module)
    function_object = getattr(module_object, function_name)

    return function_object


def get_creation_options(config: str, driver: str):
    """
    Gets a list of options for a specific format or returns None.
    :param config: The configuration for a datasource.
    :param driver: The file format to look for specific creation options.
    :return: A tuple of None or the first value is list of warp creation options, and
     the second value is a list of translate create options.     """
    if config:
        conf = yaml.safe_load(config) or dict()
        params = conf.get("formats", {}).get(driver, {})
        return params.get("warp_params"), params.get("translate_params")
    return None, None


def make_dirs(path):
    try:
        os.makedirs(path)
    except OSError:
        if not os.path.isdir(path):
            raise


def get_ogcapi_data(
    config=None,
    task_uid=None,
    stage_dir=None,
    bbox=None,
    service_url=None,
    session_token=None,
    export_format_slug=None,
    selection=None,
    download_path=None,
):
    if download_path is None:
        raise Exception("A download path is required to download ogcapi data.")

    configuration = load_provider_config(config)

    geom = get_geometry(bbox, selection)

    try:
        ogc_process = OgcApiProcess(
            url=service_url,
            config=configuration["ogcapi_process"],
            session_token=session_token,
            task_id=task_uid,
            cred_var=configuration.get("cred_var"),
            cert_info=configuration.get("cert_info"),
        )
        ogc_process.create_job(geom, file_format=export_format_slug)
        download_url = ogc_process.get_job_results()
        if not download_url:
            raise Exception("Invalid response from OGC API server")
    except Exception as e:
        raise Exception(f"Error creating OGC API Process job:{e}")

    update_progress(task_uid, progress=50, subtask_percentage=50)

    download_credentials = configuration["ogcapi_process"].get("download_credentials", dict())
    basic_auth = download_credentials.get("cred_var")
    username = password = session = cookie = None
    if basic_auth:
        username, password = os.getenv(basic_auth).split(":")
    if getattr(settings, "SITE_NAME", os.getenv("HOSTNAME")) in download_url:
        session = EventKitClient(getattr(settings, "SITE_URL"), username=username, password=password,)
        session = session.client
        cert_info = None
    else:
        cert_info = download_credentials.get("cert_info")
        cookie = download_credentials.get("cookie")
        cookie = json.loads(cookie) if cookie else None

    download_path = download_data(
        task_uid,
        download_url,
        download_path,
        username=username,
        password=password,
        session=session,
        cert_info=cert_info,
        cookie=cookie,
    )
    extract_metadata_files(download_path, stage_dir)

    return download_path
