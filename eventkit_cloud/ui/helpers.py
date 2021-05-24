import json
import math
import os
import re
import shutil
import zipfile
from datetime import datetime
from uuid import uuid4

import pytz
from celery.utils.log import get_task_logger
from django.conf import settings

from eventkit_cloud.utils.gdalutils import get_meta, convert_vector, polygonize

logger = get_task_logger(__name__)


def file_to_geojson(in_memory_file):
    """
    :param in_memory_file: A WSGI In memory file
    :return: A geojson object if available
    """
    stage_dir = settings.EXPORT_STAGING_ROOT.rstrip("\/")
    uid = str(uuid4())
    dir_name = os.path.join(stage_dir, uid)

    try:
        os.mkdir(dir_name)
        file_name = in_memory_file.name

        file_name, file_extension = os.path.splitext(file_name)
        if not file_name or not file_extension:
            raise Exception("No file type detected")

        # Remove all non-word characters
        file_name = re.sub(r"[^\w\s]", "", file_name)
        # Replace all whitespace with a single underscore
        file_name = re.sub(r"\s+", "_", file_name).lower()
        file_name = f"{file_name}-{uid}"

        in_path = os.path.join(dir_name, "in_{0}{1}".format(file_name, file_extension))
        out_path = os.path.join(dir_name, "out_{0}.geojson".format(file_name))
        write_uploaded_file(in_memory_file, in_path)

        if file_extension == ".zip":
            if unzip_file(in_path, dir_name):
                has_shp = False
                for unzipped_file in os.listdir(dir_name):
                    if unzipped_file.endswith(".shp"):
                        in_path = os.path.join(dir_name, unzipped_file)
                        has_shp = True
                        break
                if not has_shp:
                    raise Exception("Zip file does not contain a shp")

        meta = get_meta(in_path, is_raster=False)

        if not meta["driver"] or meta["is_raster"]:
            out_path = polygonize(in_path, out_path)
        else:
            out_path = convert_vector(in_path, out_path, driver="geojson")

        if os.path.exists(out_path):
            geojson = read_json_file(out_path)
            return geojson

        raise Exception("An unknown error occurred while processing the file")

    except Exception as e:
        logger.error(e)
        raise e

    finally:
        if os.path.exists(dir_name):
            shutil.rmtree(dir_name)


def read_json_file(fp):
    """
    :param fp: Path to a geojson file
    :return: A geojson object
    """
    try:
        with open(fp) as file_geojson:
            geojson = json.load(file_geojson)
            return geojson
    except Exception:
        raise Exception("Unable to read the file")


def unzip_file(fp, dir):
    """
    :param fp: Path to a zip file
    :param dir: Directory where the files should be unzipped to
    :return: True if successful
    """
    if not fp or not dir:
        return False
    try:
        zip = zipfile.ZipFile(fp, "r")
        zip.extractall(dir)
        zip.close()
        return True
    except Exception as e:
        logger.debug(e)
        raise Exception("Could not unzip file")


def write_uploaded_file(in_memory_file, write_path):
    """
    :param in_memory_file: An WSGI in memory file
    :param write_path: The path which the file should be written to on disk
    :return: True if successful
    """
    try:
        with open(write_path, "wb+") as temp_file:
            for chunk in in_memory_file.chunks():
                temp_file.write(chunk)
        return True
    except Exception as e:
        logger.debug(e)
        raise Exception("Could not write file to disk")


def set_session_user_last_active_at(request):
    # Set last active time, which is used for auto logout.
    last_active_at = datetime.utcnow().replace(tzinfo=pytz.utc)
    request.session[settings.SESSION_USER_LAST_ACTIVE_AT] = last_active_at.isoformat()

    # Return the last active datetime for convenience.
    return last_active_at


def is_mgrs(query):
    """
    :param query: A string to test against MGRS format
    :return: True if the string matches MGSR, False if not
    """
    query = re.sub(r"\s+", "", query)
    pattern = re.compile(
        r"^(\d{1,2})([C-HJ-NP-X])\s*([A-HJ-NP-Z])([A-HJ-NP-V])\s*(\d{1,5}\s*\d{1,5})$",
        re.I,
    )
    if pattern.match(query):
        return True
    return False


def is_lat_lon(query):
    """
    :param query: A string to test against lat/lon format
    :return: A parsed coordinate array if it matches, False if not
    """
    # regex for matching to lat and lon
    # ?P<name> creates a named group so that we can access the value later
    lat_lon = re.compile(
        r"""
        # group to match latitude
        (?:
            ^                       # latitude MUST start at the beginning of the string
            (?P<lat_sign>[\+-]?)    # latitude may begin with + or -
            (?:
                (?P<lat>90(?:(?:\.0{1,20})?) | (?:[0-9]|[1-8][0-9])(?:(?:\.[0-9]{1,20})?)) # match valid latitude values
                [\s]?               # there may or may not be a space following the digits when N or S are included
                (?P<lat_dir>[NS]?)  # N or S may be used instead of + or -
            )
            \b                      # there should be a word boundary after the latitude
        )

        # match common, space, or comma and space
        (?:[\,\s]{1,2})

        # group to match longitude
        (?:
            (?P<lon_sign>[\+-]?)    # longitude may begin with + or -
            (?:
                (?P<lon>180(?:(?:\.0{1,20})?)|(?:[0-9]|[1-9][0-9]|1[0-7][0-9])(?:(?:\.[0-9]{1,20})?)) # match valid longitude values
                [\s]?               # there may or may not be a space following the digits when E or W are included
                (?P<lon_dir>[EW]?)  # E or W may be used instead of + or -
            )
            $                       # after longitude should be the end of the string
        )
    """,  # NOQA
        re.VERBOSE,
    )

    r = lat_lon.match(query)
    if not r:
        return False

    parsed_lat = None
    parsed_lon = None
    try:
        parsed_lat = float(r.group("lat"))
        parsed_lon = float(r.group("lon"))
    except ValueError:
        return False

    if math.isnan(parsed_lat) or math.isnan(parsed_lon):
        return False

    if r.group("lat_sign") == "-" or r.group("lat_dir") == "S":
        parsed_lat = parsed_lat * -1
    if r.group("lon_sign") == "-" or r.group("lon_dir") == "W":
        parsed_lon = parsed_lon * -1

    parsed_coord_array = [parsed_lat, parsed_lon]

    return parsed_coord_array
