import copy
import json
import logging
import os
import pickle
import re
import requests
import requests_pkcs12
import signal
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
import yaml
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.text import slugify
from enum import Enum
from functools import reduce
from numpy import linspace
from operator import itemgetter
from typing import List, Optional, ValuesView
from xml.dom import minidom
from concurrent import futures
from pathlib import Path
from zipfile import ZipFile

from eventkit_cloud.core.helpers import get_cached_model
from eventkit_cloud.jobs.enumerations import GeospatialDataType
from eventkit_cloud.jobs.models import DataProvider, ExportFormat
from eventkit_cloud.tasks import DEFAULT_CACHE_EXPIRATION
from eventkit_cloud.tasks.exceptions import FailedException
from eventkit_cloud.tasks.models import DataProviderTaskRecord, ExportRunFile, ExportTaskRecord
from eventkit_cloud.tasks.task_process import update_progress
from eventkit_cloud.utils import auth_requests, gdalutils
from eventkit_cloud.utils.gdalutils import get_band_statistics, get_chunked_bbox
from eventkit_cloud.utils.generic import cd, get_file_paths  # NOQA

logger = logging.getLogger()


class Directory(Enum):
    ARCGIS = "arcgis"
    DATA = "data"
    TEMPLATES = "templates"


PREVIEW_TAIL = "preview.jpg"

UNSUPPORTED_CARTOGRAPHY_FORMATS = [".pbf", ".gpx"]

CHUNK = 1024 * 1024 * 2  # 2MB chunks


def get_run_staging_dir(run_uid):
    """
    The run staging dir is where all files are stored while they are being processed.
    It is a unique space to ensure that files aren't being improperly modified.
    :param run_uid: The unique value to store the directory for the run data.
    :return: The path to the run directory.
    """
    return os.path.join(settings.EXPORT_STAGING_ROOT.rstrip("\/"), str(run_uid))


def get_run_download_dir(run_uid):
    """
    The run download dir is where all files are stored after they are processed.
    It is a unique space to ensure that files aren't being improperly modified.
    :param run_uid: The unique value to store the directory for the run data.
    :return: The path to the run directory.
    """
    return os.path.join(settings.EXPORT_DOWNLOAD_ROOT.rstrip("\/"), str(run_uid))


def get_run_download_url(run_uid, provider_slug=None):
    """
    A URL path to the run data
    :param run_uid: The unique identifier for the run data.
    :return: The url context. (e.g. /downloads/123e4567-e89b-12d3-a456-426655440000)
    """
    if provider_slug:
        url = f"{settings.EXPORT_MEDIA_ROOT.rstrip('/')}/{str(run_uid)}/{provider_slug}"
    else:
        url = f"{settings.EXPORT_MEDIA_ROOT.rstrip('/')}/{str(run_uid)}"

    return url


def get_provider_staging_dir(run_uid, provider_slug):
    """
    The provider staging dir is where all files are stored while they are being processed.
    It is a unique space to ensure that files aren't being improperly modified.
    :param run_uid: The unique id for the run.
    :param provider_slug: The unique value to store the directory for the provider data.
    :return: The path to the provider directory.
    """
    run_staging_dir = get_run_staging_dir(run_uid)
    return os.path.join(run_staging_dir, provider_slug)


def get_provider_download_dir(run_uid, provider_slug):
    """
    The provider staging dir is where all files are stored after they are processed.
    It is a unique space to ensure that files aren't being improperly modified.
    :param run_uid: The unique id for the run.
    :param provider_slug: The unique value to store the directory for the provider data.
    :return: The path to the provider directory.
    """
    run_download_dir = get_run_download_dir(run_uid)
    return os.path.join(run_download_dir, provider_slug)


def get_provider_staging_preview(run_uid, provider_slug):
    """
    The provider staging dir is where all files are stored while they are being processed.
    It is a unique space to ensure that files aren't being improperly modified.
    :param run_uid: The unique id for the run.
    :param provider_slug: The unique value to store the directory for the provider data.
    :return: The path to the provider directory.
    """
    run_staging_dir = get_run_staging_dir(run_uid)
    return os.path.join(run_staging_dir, provider_slug, PREVIEW_TAIL)


def get_download_filename(name: str, ext: str, additional_descriptors: List[str] = None):
    """
    This provides specific formatting for the names of the downloadable files.
    :param name: A name for the file, typically the job name.
    :param ext: The file extension (e.g. .gpkg)
    :param additional_descriptors: Additional descriptors, any list of items.
    :return: The formatted file name (e.g. Boston-example-20180711.gpkg)
    """
    download_filename = f"{'-'.join(filter(None, [name] + (additional_descriptors or [])))}{ext}"
    return download_filename


def get_archive_data_path(provider_slug=None, file_name=None, archive=True):
    """
    Gets a datapath for the files to be placed in the zip file.
    :param provider_slug: An optional unique value to store files.
    :param file_name: The name of a file.
    :return:
    """
    if archive:
        file_path = Directory.DATA.value
    else:
        file_path = ""

    if provider_slug:
        file_path = os.path.join(file_path, provider_slug)
    if file_name:
        file_path = os.path.join(file_path, file_name)
    return file_path


def default_format_time(date_time):
    return date_time.strftime("%Y%m%d")


def normalize_name(name):
    # Remove all non-word characters
    s = re.sub(r"[^\w\s]", "", name)
    # Replace all whitespace with a single underscore
    s = re.sub(r"\s+", "_", s)
    return s.lower()


def get_export_task_record(export_task_record_uid: str) -> ExportTaskRecord:
    """
    Gets the ExportTaskRecord and related models used for export_tasks from the ExportTaskRecord.
    :param export_task_record_uid: The UID of an ExportTaskRecord.
    :return provider_slug: The associated provider_slug value.
    """
    return ExportTaskRecord.objects.select_related("export_provider_task__provider").get(uid=export_task_record_uid)


def get_supported_projections(format_slug: str) -> List[int]:
    supported_projections = (
        ExportFormat.objects.get(slug=format_slug).supported_projections.all().values_list("srid", flat=True)
    )
    return supported_projections


def get_default_projection(supported_projections: List[int], selected_projections: List[int]) -> Optional[int]:
    """
    Gets a default projection of either 4326 or the first supported projection.
    """
    if 4326 in supported_projections and 4326 in selected_projections:
        return 4326
    for supported_projection in supported_projections:
        if supported_projection in selected_projections:
            return supported_projection
    return None


def get_data_provider_label(data_provider_slug):
    try:
        data_provider = get_cached_model(model=DataProvider, prop="slug", value=data_provider_slug)
        return slugify(data_provider.label or "")  # Slugify converts None to 'none' so return empty string instead.
    except DataProvider.DoesNotExist:
        logger.info(f"{data_provider_slug} does not map to any known DataProvider.")
        raise


def get_export_filepath(stage_dir: str, job_name: str, projection: int, data_provider_slug: str, extension: str):
    """
    Gets a filepath for an export.
    :param stage_dir: The staging directory to place files in while they process.
    :param job_name: The name of the job being processed.
    :param projection: A projection as an int referencing an EPSG code (e.g. 4326 = EPSG:4326)
    :param data_provider_slug: The provider slug (e.g. osm) for the filename.
    :param extension: The file extension for the filename.
    """
    descriptors = "-".join(
        filter(
            None,
            [
                job_name,
                str(projection),
                data_provider_slug,
                get_data_provider_label(data_provider_slug),
                default_format_time(time),
            ],
        )
    )
    if extension == "shp":
        filepath = os.path.join(stage_dir, f"{descriptors}_{extension}")
    else:
        filepath = os.path.join(stage_dir, f"{descriptors}.{extension}")

    return filepath


def get_style_files():
    """

    :return: A list of all of the static files used for styles (e.g. icons)
    """
    style_dir = os.path.join(os.path.dirname(__file__), "static", "tasks", "styles")
    files = get_file_paths(style_dir)
    arcgis_dir = os.path.join(os.path.dirname(__file__), "arcgis")
    files = get_file_paths(arcgis_dir, files)
    return files


def create_license_file(provider_task):
    # checks a DataProviderTaskRecord's license file and adds it to the file list if it exists
    data_provider_license = DataProvider.objects.get(slug=provider_task.provider.slug).license

    # DataProviders are not required to have a license
    if data_provider_license is None:
        return

    license_file_path = os.path.join(
        get_provider_staging_dir(provider_task.run.uid, provider_task.provider.slug),
        "{0}.txt".format(normalize_name(data_provider_license.name)),
    )

    with open(license_file_path, "wb") as license_file:
        license_file.write(data_provider_license.text.encode())

    return license_file_path


def generate_qgs_style(metadata, skip_formats=UNSUPPORTED_CARTOGRAPHY_FORMATS):
    """
    Task to create QGIS project file with styles for osm.

    If a data_provider_task_record is provided a style file will be generated only for that, otherwise all of the
    data providers in the run will be added to the style file.
    :param metadata: A dict of metadata provided by get_metadata.
    :return: The path to the generated qgs file.
    """

    from eventkit_cloud.tasks.helpers import normalize_name

    cleaned_metadata = remove_formats(metadata, formats=UNSUPPORTED_CARTOGRAPHY_FORMATS)

    stage_dir = os.path.join(settings.EXPORT_STAGING_ROOT, str(cleaned_metadata["run_uid"]))

    job_name = normalize_name(cleaned_metadata["name"].lower())

    provider_details = [provider_detail for provider_slug, provider_detail in cleaned_metadata["data_sources"].items()]

    if len(provider_details) == 1:
        style_file_name = "{0}-{1}-{2}.qgs".format(
            job_name,
            normalize_name(provider_details[0]["slug"]),
            default_format_time(timezone.now()),
        )
    else:
        style_file_name = "{0}-{1}.qgs".format(job_name, default_format_time(timezone.now()))

    style_file = os.path.join(stage_dir, style_file_name)

    context = {
        "job_name": job_name,
        "job_date_time": "{0}".format(timezone.now().strftime("%Y%m%d%H%M%S%f")[:-3]),
        "provider_details": provider_details,
        "bbox": metadata["bbox"],
        "has_raster": metadata["has_raster"],
        "has_elevation": metadata["has_elevation"],
        "has_vector": metadata["has_vector"],
    }

    with open(style_file, "wb") as open_file:
        open_file.write(
            render_to_string(
                "styles/Style.qgs",
                context=context,
            ).encode()
        )
    return style_file


def remove_formats(metadata: dict, formats: List[str] = UNSUPPORTED_CARTOGRAPHY_FORMATS):
    """
    Used to remove formats from the metadata especially so that they don't show up in the cartography.
    :param data_sources: A dict of metadata provided by get_metadata.
    :param formats: A list of unsupported file extensions (i.e. .gpx)
    :return: The path to the generated qgs file.
    """
    # Create a new dict to not alter the input data.
    if metadata is None:
        metadata = {}
    cleaned_metadata = copy.deepcopy(metadata)
    for slug, data_source in cleaned_metadata.get("data_sources", {}).items():
        cleaned_metadata["data_sources"][slug] = data_source
        cleaned_files = []
        for file_info in cleaned_metadata["data_sources"][slug].get("files"):
            # Add all files that aren't in the remove list.
            if file_info.get("file_ext") not in formats:
                cleaned_files.append(file_info)
        cleaned_metadata["data_sources"][slug]["files"] = cleaned_files
    return cleaned_metadata


def get_human_readable_metadata_document(metadata):
    """

    :param metadata: A dictionary returned by get_metadata.
    :return: A filepath to a txt document.
    """
    from eventkit_cloud.tasks.helpers import normalize_name

    stage_dir = os.path.join(settings.EXPORT_STAGING_ROOT, str(metadata["run_uid"]))

    metadata_file = os.path.join(stage_dir, "{0}_ReadMe.txt".format(normalize_name(metadata["name"])))

    with open(metadata_file, "wb") as open_file:
        open_file.write(
            render_to_string("styles/metadata.txt", context={"metadata": metadata})
            .replace("\r\n", "\n")
            .replace("\n", "\r\n")
            .encode()
        )
    return metadata_file


def get_last_update(url, type, cert_info=None):
    """
    A wrapper to get different timestamps.
    :param url: The url to get the timestamp
    :param type: The type of services (e.g. osm)
    :param cert_info: Optionally a dict containing cert path and pass
    :return: The timestamp as a string.
    """
    if type == "osm":
        return get_osm_last_update(url, cert_info=cert_info)


def get_metadata_url(url, type):
    """
    A wrapper to get different timestamps.
    :param url: The url to get the timestamp
    :param type: The type of services (e.g. osm)
    :return: The timestamp as a string.
    """
    if type in ["wcs", "wms", "wmts"]:
        return "{0}?request=GetCapabilities".format(url.split("?")[0])
    else:
        return url


def get_osm_last_update(url, cert_info=None):
    """

    :param url: A path to the overpass api.
    :param cert_info: Optionally cert info if needed
    :return: The default timestamp as a string (2018-06-18T13:09:59Z)
    """
    try:
        timestamp_url = "{0}timestamp".format(url.rstrip("/").rstrip("interpreter"))
        response = auth_requests.get(timestamp_url, cert_info=cert_info)
        if response:
            return response.content.decode()
        raise Exception("Get OSM last update failed with {0}: {1}".format(response.status_code, response.content))
    except Exception as e:
        logger.warning(e)
        logger.warning("Could not get the timestamp from the overpass url.")
        return None


def progressive_kill(pid):
    """
    Tries to kill first with TERM and then with KILL.
    :param pid: The process ID to kill
    :return: None.
    """
    try:
        logger.info("Trying to terminate process group {0} with SIGTERM.".format(pid))
        os.killpg(pid, signal.SIGTERM)
        time.sleep(5)

        logger.info("Trying to kill process group {0} with SIGKILL.".format(pid))
        os.killpg(pid, signal.SIGKILL)
        time.sleep(1)

    except OSError:
        logger.info("{0} PID no longer exists.".format(pid))


def pickle_exception(exception):
    return pickle.dumps(exception, 0).decode()


def get_metadata(data_provider_task_record_uids: List[str], source_only=False):
    """
    A object to hold metadata about the run for the sake of being passed to various scripts for the creation of
    style files or metadata documents for within the datapack.

    This also creates a license file which is considered a part of the metadata and adds it to the "include_files"
    :param source_only: If enabled only the first task for the data_provider_task_record will be included in the
    metadata.  This is useful for generating style files for a single layer instead of redundant layers for each file.
    :param data_provider_task_record_uids: A list of Provider task uid string for either the run task which will add
    all of the provider tasks for the run or for a single data provider task.
    :return: A dict containing the run metadata.

    Example:
    {
    "aoi": "GEOJSON representing the selected AOI."
    "bbox": [
        w, s, e, n
    ],
    "data_sources": {
        "osm": {
            "copyright": None,
            "description": "OpenStreetMap vector data provided in a custom thematic schema. \r\n\t\r\n\tData is
            grouped into separate tables (e.g. water, roads...).",
            "files": [{"file_path": "data/osm/test-osm-20181101.gpkg",
                       "file_ext": ".gpkg",
                       "full_file_path": "/var/lib/eventkit/exports_stage/7fadf34e-58f9-4bb8-ab57-adc1015c4269
                       /osm/test.gpkg",
                       "band_stats":
                       "ramp_shader_steps":}]
            "last_update": "2018-10-29T04:35:02Z\n",
            "metadata": "https://overpass-server.com/overpass/interpreter",
            "name": "OpenStreetMap Data (Themes)",
            "slug": "osm",
            "type": "osm",
            "uid": "0d08ddf6-35c1-464f-b271-75f6911c3f78",
            "layers": ["layer1", "layer2"]
        }
    },
    "date": "20181101",
    "description": "Test",
    "has_elevation": False,
    "has_raster": True,
    "include_files": [
        "/var/lib/eventkit/exports_stage/7fadf34e-58f9-4bb8-ab57-adc1015c4269/osm/test.gpkg",
        "/var/lib/eventkit/exports_stage/7fadf34e-58f9-4bb8-ab57-adc1015c4269/osm/osm_selection.geojson"
    ],
    "name": "test",
    "project": "Test",
    "projections": [4326, 3857]
    "run_uid": "7fadf34e-58f9-4bb8-ab57-adc1015c4269",
    "url": "http://cloud.eventkit.test/status/2010025c-6d61-4a0b-8d5d-ff9c657259eb"
    }
    """

    from eventkit_cloud.tasks.enumerations import TaskState
    from eventkit_cloud.tasks.export_tasks import create_zip_task

    data_provider_task_records = (
        DataProviderTaskRecord.objects.select_related("run__job")
        .prefetch_related("run__job__projections")
        .prefetch_related("provider")
        .filter(uid__in=data_provider_task_record_uids)
    )
    run = data_provider_task_records.first().run

    projections = []
    for projection in run.job.projections.all():
        projections.append(projection.srid)

    # To prepare for the zipfile task, the files need to be checked to ensure they weren't
    # deleted during cancellation.
    include_files = list([])

    # A dict is used here to ensure that just one file per provider is added,
    # this should be updated when multiple formats are supported.
    metadata = {
        "name": normalize_name(run.job.name),
        "url": "{0}/status/{1}".format(getattr(settings, "SITE_URL"), str(run.job.uid)),
        "description": run.job.description,
        "project": run.job.event,
        "projections": projections,
        "date": timezone.now().strftime("%Y%m%d"),
        "run_uid": str(run.uid),
        "data_sources": {},
        "bbox": run.job.extents,
        "aoi": run.job.bounds_geojson,
        "has_raster": False,  # TODO: These are used for style groupings and seem frivolous.
        "has_elevation": False,
        "has_vector": False,
    }
    for data_provider_task_record in data_provider_task_records:
        data_provider = data_provider_task_record.provider
        provider_type = data_provider.export_provider_type.type_name

        provider_staging_dir = get_provider_staging_dir(run.uid, data_provider_task_record.provider.slug)
        conf = yaml.safe_load(data_provider.config) or dict()
        cert_info = conf.get("cert_info", None)
        metadata["data_sources"][data_provider_task_record.provider.slug] = {
            "uid": str(data_provider_task_record.uid),
            "slug": data_provider_task_record.provider.slug,
            "name": data_provider_task_record.name,
            "files": [],
            "type": get_data_type_from_provider(data_provider_task_record.provider),
            "description": str(data_provider.service_description).replace("\r\n", "\n").replace("\n", "\r\n\t"),
            "last_update": get_last_update(data_provider.url, provider_type, cert_info=cert_info),
            "metadata": get_metadata_url(data_provider.url, provider_type),
            "copyright": data_provider.service_copyright,
            "layers": data_provider.layers,
            "level_from": data_provider.level_from,
            "level_to": data_provider.level_to,
        }
        if (
            metadata["data_sources"][data_provider_task_record.provider.slug].get("type")
            == GeospatialDataType.RASTER.value
        ):
            metadata["has_raster"] = True
        if (
            metadata["data_sources"][data_provider_task_record.provider.slug].get("type")
            == GeospatialDataType.ELEVATION.value
        ):
            metadata["has_elevation"] = True
        if metadata["data_sources"][data_provider_task_record.provider.slug].get("type") in [
            GeospatialDataType.VECTOR.value,
            "osm",
            "nome",
        ]:  # TODO: handle osm generically like vector layers
            metadata["has_vector"] = True

        if data_provider_task_record.preview is not None:
            include_files += [get_provider_staging_preview(run.uid, data_provider_task_record.provider.slug)]

        # Only include tasks with a specific projection in the metadata.
        # TODO: Refactor to make explicit which files are included in map documents.
        query = reduce(lambda q, value: q | Q(name__icontains=value), projections, Q())
        export_tasks = data_provider_task_record.tasks.filter(query)
        if source_only:
            export_tasks = [export_tasks.first()]
        for export_task in export_tasks:

            if TaskState[export_task.status] in TaskState.get_incomplete_states():
                continue

            try:
                filename = export_task.result.filename
            except Exception:
                continue
            full_file_path = os.path.join(provider_staging_dir, filename)
            current_files = metadata["data_sources"][data_provider_task_record.provider.slug]["files"]

            if full_file_path not in map(itemgetter("full_file_path"), current_files):
                file_ext = os.path.splitext(filename)[1]
                # Only include files relevant to the user that we can actually add to the carto.
                if export_task.display and ("project file" not in export_task.name.lower()):
                    download_filename = get_download_filename(os.path.splitext(os.path.basename(filename))[0], file_ext)

                    filepath = get_archive_data_path(
                        data_provider_task_record.provider.slug, download_filename, archive=(not source_only)
                    )
                    pattern = re.compile(".*EPSG:(?P<projection>3857|4326).*$")
                    matches = pattern.match(export_task.name)
                    projection = "4326"
                    if matches:
                        projection = pattern.match(export_task.name).groupdict().get("projection")
                    file_data = {
                        "file_path": filepath,
                        "full_file_path": full_file_path,
                        "file_ext": file_ext,
                        "projection": projection,
                    }
                    if (
                        metadata["data_sources"][data_provider_task_record.provider.slug].get("type")
                        == GeospatialDataType.ELEVATION.value
                    ):
                        # Get statistics to update ranges in template.
                        band_stats = get_band_statistics(full_file_path)
                        logger.info("Band Stats {0}: {1}".format(full_file_path, band_stats))
                        file_data["band_stats"] = band_stats
                        # Calculate the value for each elevation step (of 16)
                        try:
                            steps = linspace(band_stats[0], band_stats[1], num=16)
                            file_data["ramp_shader_steps"] = list(map(int, steps))
                        except TypeError:
                            file_data["ramp_shader_steps"] = None

                    metadata["data_sources"][data_provider_task_record.provider.slug]["files"] += [file_data]

            if not os.path.isfile(full_file_path):
                logger.error("Could not find file {0} for export {1}.".format(full_file_path, export_task.name))
                continue
            # Exclude zip files created by zip_export_provider
            if not (full_file_path.endswith(".zip") and export_task.name == create_zip_task.name):
                include_files += [full_file_path]

        # add the license for this provider if there are other files already
        license_file = create_license_file(data_provider_task_record)
        if license_file:
            include_files += [license_file]

        metadata["include_files"] = include_files

    return metadata


def get_arcgis_metadata(metadata):
    """
    A way to add or remove information which will be used by the arcgis script.
    :param metadata: A metadata dict returned from get_metadata
    :return: A metadata dict to be provided within the datapack.
    """
    arcgis_metadata = remove_formats(metadata, formats=UNSUPPORTED_CARTOGRAPHY_FORMATS)

    # remove files which reference the server directories.
    arcgis_metadata.pop("include_files")
    for data_source, data_source_values in arcgis_metadata["data_sources"].items():
        for file_details in data_source_values["files"]:
            file_details.pop("full_file_path", "")

    return arcgis_metadata


def get_data_type_from_provider(data_provider: DataProvider) -> str:
    """
    This is used to populate the run metadata with special types for OSM and NOME.  This is used for custom cartography,
    and should be removed if custom cartography is made configurable.
    :param data_provider:
    :return:
    """
    if data_provider.slug.lower() in ["nome", "osm"]:
        return data_provider.slug.lower()
    else:
        return data_provider.data_type


def get_all_rabbitmq_objects(api_url: str, rabbit_class: str) -> list:
    """
    :param api_url: The http api url including authentication values.
    :param rabbit_class: The type of rabbitmq class (i.e. queues or exchanges) as a string.
    :return: An array of dicts with the desired objects.
    """
    url = f"{api_url.rstrip('/')}/{rabbit_class}"
    params = {"page": 1, "page_size": 100, "pagination": True}
    response = None
    try:
        logger.info(f"Getting all {rabbit_class}")
        response = requests.get(url, params=params).json()
        rabbit_objects = response["items"]
        pages = response.get("page_count", 0)
        for page in range(2, pages + 1):
            logger.info(f"Getting page: {page} of {pages} for {rabbit_class}")
            params["page"] = page
            response = requests.get(url, params=params)
            if response.ok:
                rabbit_objects += response.json()["items"]
            else:
                raise Exception(f"Failed to fetch {rabbit_class}")
        return rabbit_objects
    except Exception as e:
        if response:
            logger.error(response.content.decode())
        logger.error(e)
        raise e


def delete_rabbit_objects(api_url: str, rabbit_classes: list = ["queues"], force: bool = False) -> None:
    api_url = api_url.rstrip("/")
    for rabbit_class in rabbit_classes:
        for rabbit_object in get_all_rabbitmq_objects(api_url, rabbit_class):
            object_name = urllib.parse.quote(rabbit_object.get("name"), safe="")
            vhost = urllib.parse.quote(rabbit_object.get("vhost"), safe="")
            # Exchanges don't have consumers or messages, so deleting exchanges is always done.
            consumers = rabbit_object.get("consumers")
            messages = rabbit_object.get("messages")
            if not (messages or consumers) or force:
                object_url = f"{api_url}/{rabbit_class}/{vhost}/{object_name}"
                res = requests.delete(object_url)
                if res.ok:
                    logger.info(f"Removed {rabbit_class}: {object_name}")
                else:
                    logger.info(f"Could not remove {rabbit_class} {object_name}: {res.content}")
            else:
                logger.info(f"Cannot remove {rabbit_class}: {rabbit_object}")
                if consumers:
                    logger.info(f"There are {consumers} consumers")
                if messages:
                    logger.info(f"There are {messages} messages")


def get_message_count(queue_name: str) -> int:
    """
    :param queue_name: The queue that you want to check messages for.
    :return: An integer count of pending messages.
    """
    broker_api_url = getattr(settings, "BROKER_API_URL")
    queue_class = "queues"

    for queue in get_all_rabbitmq_objects(broker_api_url, queue_class):
        if queue.get("name") == queue_name:
            try:
                return queue.get("messages", 0)
            except Exception as e:
                logger.info(e)

    logger.info(f"Cannot find queue named {queue_name}, returning 0 messages.")
    return 0


def check_cached_task_failures(task_name, task_uid):
    """
    Used to check how many times this task has already attempted to run.
    If the task continues to fail, this will fire an exception to be
    handled by the task.
    """
    cache_key = f"{task_uid}-task-attempts"
    task_attempts = cache.get_or_set(cache_key, 0)
    task_attempts += 1
    cache.set(cache_key, task_attempts)
    if task_attempts > settings.MAX_TASK_ATTEMPTS:
        raise FailedException(task_name=task_name)


def add_export_run_files_to_zip(zipfile, run_zip_file):
    """
    Add additional files stored in ExportRunFile objects to a zipfile.
    """
    if not os.path.exists(settings.EXPORT_RUN_FILES):
        os.makedirs(settings.EXPORT_RUN_FILES)

    export_run_files = ExportRunFile.objects.all()
    for export_run_file in export_run_files:
        run_zip_file.message = f"Adding {export_run_file.file.name} to zip archive."
        export_run_file_path = os.path.join(settings.EXPORT_RUN_FILES, export_run_file.file.name)

        if settings.USE_S3:
            request = requests.get(export_run_file.file.url)
            with open(export_run_file_path, "wb+") as file:
                file.write(request.content)

        extra_directory = export_run_file.directory or ""
        if export_run_file.provider:
            arcname = os.path.join("data", export_run_file.provider.slug, extra_directory, export_run_file.file.name)
            zipfile.write(export_run_file_path, arcname=arcname)
        else:
            arcname = os.path.join(extra_directory, export_run_file.file.name)
            zipfile.write(export_run_file_path, arcname)


def get_data_package_manifest(metadata: dict, ignore_files: list) -> str:
    """
    Uses a metadata to generate a manifest file.

    <MissionPackageManifest version="2">
       <Configuration>
          <Parameter name="uid" value="<UID>"/>
          <Parameter name="name" value="<Name>"/>
       </Configuration>
       <Contents>
          <Content ignore="false" zipEntry="<file_path>">
             <Parameter name="contentType" value="External Native Data"/>
          </Content>
       </Contents>
    </MissionPackageManifest>

    :param metadata: A dict of run contents.
    :param ignore_files: A list of files to ignore.
    :return: File path to manifest file.
    """
    from eventkit_cloud.tasks.helpers import normalize_name

    # Placeholder to add unsupported formats.
    cleaned_metadata = remove_formats(metadata, formats=[])

    if cleaned_metadata:
        run_uid = cleaned_metadata.get("run_uid")
        job_name = normalize_name(cleaned_metadata["name"].lower())
    else:
        run_uid = uuid.uuid4()
        job_name = "DataPack"

    stage_dir = os.path.join(settings.EXPORT_STAGING_ROOT, str(run_uid))

    root = ET.Element("MissionPackageManifest", attrib={"version": "2"})

    # Set up configuration
    configuration = ET.SubElement(root, "Configuration")
    ET.SubElement(configuration, "Parameter", attrib={"name": "uid", "value": run_uid})
    # use the first 30 characters from the name
    ET.SubElement(configuration, "Parameter", attrib={"name": "name", "value": job_name[:30]})

    # Add contents
    contents = ET.SubElement(root, "Contents")
    for data_source_slug, data_source_info in cleaned_metadata.get("data_sources", {}).items():
        data_source_type = data_source_info["type"]
        for data_file in data_source_info["files"]:
            file_path = os.path.relpath(data_file["file_path"])
            content = ET.SubElement(contents, "Content", attrib={"ignore": "false", "zipEntry": file_path})
            if data_source_type == GeospatialDataType.RASTER.value:
                # Let application know that this is raster data.
                ET.SubElement(content, "Parameter", attrib={"name": "contentType", "value": "External Native Data"})
    # Ignore contents
    for data_file in ignore_files:
        file_path = os.path.relpath(data_file)
        ET.SubElement(contents, "Content", attrib={"ignore": "true", "zipEntry": file_path})

    ET.SubElement(contents, "Content", attrib={"ignore": "false", "zipEntry": os.path.join("manifest", "manifest.xml")})

    # Pretty print using xml dom
    manifest_file = os.path.join(stage_dir, "manifest.xml")
    manifest = minidom.parseString(ET.tostring(root)).toprettyxml(indent="   ")

    # Strip the header (and newline) that minidom forces.  Consider lxml in future.
    manifest = "\n".join(manifest.split("\n")[1:-1])

    if not os.path.isdir(os.path.dirname(manifest_file)):
        os.makedirs(os.path.dirname(manifest_file))

    with open(manifest_file, "w") as open_file:
        open_file.write(manifest)
    return manifest_file


def merge_chunks(
    output_file,
    layer_name,
    projection,
    task_uid: str,
    bbox: list,
    stage_dir: str,
    base_url: str,
    cert_info=None,
    task_points=100,
    feature_data=False,
    distinct_field=None,
):
    chunks = download_chunks(task_uid, bbox, stage_dir, base_url, cert_info, task_points, feature_data)
    out = gdalutils.convert(
        driver="gpkg",
        input_file=chunks,
        output_file=output_file,
        task_uid=task_uid,
        boundary=bbox,
        layer_name=layer_name,
        projection=projection,
        access_mode="append",
        distinct_field=distinct_field,
    )
    return out


def download_chunks_concurrently(layer, task_points, feature_data):
    base_path = layer.get("base_path")
    if not os.path.exists(base_path):
        os.mkdir(base_path)
    merge_chunks(
        output_file=layer.get("path"),
        projection=layer.get("projection"),
        layer_name=layer.get("layer_name"),
        task_uid=layer.get("task_uid"),
        bbox=layer.get("bbox"),
        stage_dir=base_path,
        base_url=layer.get("url"),
        cert_info=layer.get("cert_info"),
        task_points=task_points,
        feature_data=feature_data,
        distinct_field=layer.get("distinct_field"),
    )


def download_concurrently(layers: ValuesView, concurrency=None, feature_data=False):
    """
    Function concurrently downloads data from a given list URLs and download paths.
    """

    try:
        executor = futures.ThreadPoolExecutor(max_workers=concurrency)

        # Get the total number of task points to compare against current progress.
        task_points = len(layers) * 100

        futures_list = [
            executor.submit(
                download_chunks_concurrently, layer=layer, task_points=task_points, feature_data=feature_data
            )
            for layer in layers
        ]
        futures.wait(futures_list)

        # result() is called for all futures so that any exception raised within is propagated to the caller.
        [ftr.result() for ftr in futures_list]

    except Exception as e:
        logger.error(f"Unable to execute concurrent downloads: {e}")
        raise e

    return layers


@gdalutils.retry
def download_feature_data(task_uid: str, input_url: str, out_file: str, cert_info=None, task_points=100):
    # This function is necessary because ArcGIS servers often either
    # respond with a 200 status code but also return an error message in the response body,
    # or redirect to a parent URL if a resource is not found.

    try:
        out_file = download_data(task_uid, input_url, out_file, cert_info, task_points)
        with open(out_file) as f:
            json_response = json.load(f)

            if json_response.get("error"):
                logger.error(json_response)
                raise Exception("The service did not receive a valid response.")

            if "features" not in json_response:
                logger.error(f"No features were returned for {input_url}")
                raise Exception("No features were returned.")

    except Exception as e:
        logger.error(f"Feature data download error: {e}")
        raise e

    return out_file


def download_chunks(
    task_uid: str,
    bbox: list,
    stage_dir: str,
    base_url: str,
    cert_info=None,
    task_points=100,
    feature_data=False,
    level=15,
):
    tile_bboxes = get_chunked_bbox(bbox, level=level)
    chunks = []
    for _index, _tile_bbox in enumerate(tile_bboxes):
        # Replace bbox placeholder here, allowing for the bbox as either a list or tuple
        url = base_url.replace("BBOX_PLACEHOLDER", urllib.parse.quote(str([*_tile_bbox]).strip("[]")))
        outfile = os.path.join(stage_dir, f"chunk{_index}.json")

        download_function = download_feature_data if feature_data else download_data
        download_function(task_uid, url, outfile, cert_info, task_points=task_points)
        chunks.append(outfile)
    return chunks


def get_or_update_session(username=None, password=None, session=None, max_retries=3, verify=True, cert_info=None):
    if not session:
        session = requests.Session()

    if username and password:
        logger.error(f"setting {username} and {password} for session")
        session.auth = (username, password)
        adapter = requests.adapters.HTTPAdapter(max_retries=max_retries)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

    cert_path, cert_pass = auth_requests.get_cert_info({"cert_info": cert_info})
    if cert_path and cert_pass:
        adapter = requests_pkcs12.Pkcs12Adapter(
            pkcs12_filename=cert_path,
            pkcs12_password=cert_pass,
            max_retries=max_retries,
        )
        session.mount("https://", adapter)

    logger.debug("Using %s for SSL verification.", str(verify))
    session.verify = verify
    return session


def download_data(task_uid: str, input_url: str, out_file: str, cert_info=None, task_points=100, session=None):
    """
    Function for downloading data, optionally using a certificate.
    """
    try:
        session = get_or_update_session(
            session=session, cert_info=cert_info, verify=getattr(settings, "SSL_VERIFICATION", True)
        )
        response = session.get(input_url, stream=True)
        response.raise_for_status()

    except requests.exceptions.RequestException as e:
        raise Exception(f"Unsuccessful request:{e}")

    from audit_logging.file_logging import logging_open

    try:
        total_size = int(response.headers.get("content-length"))
    except (ValueError, TypeError):
        if response.content:
            total_size = len(response.content)
        else:
            raise Exception("Request failed to return any data.")

    try:
        content_type = response.headers.get("content-type")
        if Path(out_file).suffix.replace(".", "") not in content_type:
            raise Exception("The returned data is not in the expected format.")
    except Exception:
        logger.error("Unable to verify data type.")

    written_size = 0
    update_interval = total_size / 100
    cache.set(get_task_progress_cache_key(task_uid), 0, timeout=DEFAULT_CACHE_EXPIRATION)

    with logging_open(out_file, "wb") as file_:
        for chunk in response.iter_content(CHUNK):
            file_.write(chunk)
            written_size += CHUNK

            last_update = cache.get_or_set(get_last_update_cache_key(task_uid), 0)
            last_update += CHUNK
            cache.set(get_last_update_cache_key(task_uid), last_update, timeout=DEFAULT_CACHE_EXPIRATION)

            if last_update > update_interval:
                updated_points = int((last_update / total_size) * 100) if last_update < total_size else 100
                cache.incr(get_task_progress_cache_key(task_uid), updated_points)

                progress_points = cache.get(get_task_progress_cache_key(task_uid))
                progress = progress_points / task_points * 100 if progress_points < task_points else 100
                update_progress(task_uid, progress)

                cache.set(get_last_update_cache_key(task_uid), 0, timeout=DEFAULT_CACHE_EXPIRATION)

    if not os.path.isfile(out_file):
        raise Exception("Nothing was returned from the vector feature service.")

    return out_file


def get_task_points_cache_key(task_uid: str):
    return f"{task_uid}_task_points"


def get_task_progress_cache_key(task_uid: str):
    return f"{task_uid}_progress"


def get_last_update_cache_key(task_uid: str):
    return f"{task_uid}_mb_since_update"


def find_in_zip(
    zip_filepath: str, extension: str, stage_dir: str, archive_extension: str = "zip", matched_files: list = list()
):
    """
    Function finds files within archives and returns their vsi path.
    """
    zip_file = ZipFile(zip_filepath)
    files_in_zip = zip_file.namelist()
    extension = extension.lower()

    for filepath in files_in_zip:
        file_path = Path(filepath)

        if extension in file_path.suffix.lower() and file_path not in matched_files:
            return f"/vsizip/{zip_filepath}/{filepath}"

        if archive_extension in file_path.suffix:
            nested = Path(f"{stage_dir}/{filepath}")
            nested.parent.mkdir(parents=True, exist_ok=True)
            with open(nested, "wb") as f:
                f.write(zip_file.read(filepath))

            return find_in_zip(nested.absolute(), extension, stage_dir, matched_files=matched_files)


def extract_metadata_files(
    zip_filepath: str,
    destination: str,
    extensions: list = [".md", ".txt", ".doc", ".docx", ".csv", ".xls", ".xlsx"],
):
    """
    Function extract metadata files from archives.
    The function will look for any files that match the extensions that were provided,
    and will extract those files into a metadata directory.
    """

    zip_file = ZipFile(zip_filepath)
    files_in_zip = zip_file.namelist()

    metadata_dir = Path(f"{destination}/metadata/")
    metadata_dir.mkdir(parents=True, exist_ok=True)

    for filepath in files_in_zip:
        file_path = Path(filepath)

        if file_path.suffix in extensions:
            zip_file.extract(filepath, path=metadata_dir)

    return str(metadata_dir)
