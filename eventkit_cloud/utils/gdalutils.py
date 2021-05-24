# -*- coding: utf-8 -*-
import json
import logging
from typing import List, Tuple

import math
import multiprocessing
import os
import time
from tempfile import NamedTemporaryFile
from functools import wraps

from mapproxy.grid import tile_grid
from osgeo import gdal, ogr, osr


from eventkit_cloud.tasks.task_process import TaskProcess, update_progress
from eventkit_cloud.utils.generic import requires_zip, create_zip_file, get_zip_name
from eventkit_cloud.utils.geocoding.geocode import GeocodeAdapter, is_valid_bbox
from django.conf import settings

logger = logging.getLogger(__name__)

MAX_DB_CONNECTION_RETRIES = 8
TIME_DELAY_BASE = 2  # Used for exponential delays (i.e. 5^y) at 8 would be about 4 minutes 15 seconds max delay.

# The retry here is an attempt to mitigate any possible dropped connections. We chose to do a limited number of
# retries as retrying forever would cause the job to never finish in the event that the database is down. An
# improved method would perhaps be to see if there are connection options to create a more reliable connection.
# We have used this solution for now as I could not find options supporting this in the gdal documentation.


GOOGLE_MAPS_FULL_WORLD = [-20037508.342789244, -20037508.342789244, 20037508.342789244, 20037508.342789244]


def retry(f):
    @wraps(f)
    def wrapper(*args, **kwds):

        attempts = MAX_DB_CONNECTION_RETRIES
        exc = None
        while attempts:
            try:
                return_value = f(*args, **kwds)
                if not return_value:
                    logger.error("The function {0} failed to return any values.".format(getattr(f, "__name__")))
                    raise Exception("The process failed to return any data, please contact an administrator.")
                return return_value
            except Exception as e:
                logger.error("The function {0} threw an error.".format(getattr(f, "__name__")))
                logger.error(str(e))
                exc = e

                if getattr(settings, "TESTING", False):
                    # Don't wait/retry when running tests.
                    break
                attempts -= 1
                if "canceled" in str(e).lower():
                    # If task was canceled (as opposed to fail) don't retry.
                    attempts = 0
                else:
                    if attempts:
                        delay = TIME_DELAY_BASE ** (MAX_DB_CONNECTION_RETRIES - attempts + 1)
                        logger.error(f"Retrying {str(attempts)} more times, sleeping for {delay}...")
                        time.sleep(delay)
        raise exc

    return wrapper


def progress_callback(pct, msg, user_data):
    update_progress(
        user_data.get("task_uid"),
        progress=round(pct * 100),
        subtask_percentage=user_data.get("subtask_percentage", 100.0),
        msg=msg,
    )


def open_dataset(file_path, is_raster):
    """
    Given a path to a raster or vector dataset, returns an opened GDAL or OGR dataset.
    The caller has the responsibility of closing/deleting the dataset when finished.
    :param file_path: Path to dataset
    :return: Handle to open dataset
    """

    # Attempt to open as gdal dataset (raster)
    # Using gdal exception to minimize output to stdout
    gdal.UseExceptions()

    logger.info("Opening the dataset: {}".format(file_path))
    gdal_dataset = None
    ogr_dataset = None
    try:
        try:
            gdal_dataset = gdal.Open(file_path)
        except Exception as e:
            logger.debug("Could not open dataset using gdal as raster.")
            logger.debug(e)
        finally:
            if gdal_dataset and is_raster:
                logger.info(f"The dataset: {file_path} opened with gdal.")
                return gdal_dataset

        # Attempt to open as ogr dataset (vector)
        # ogr.UseExceptions doesn't seem to work reliably, so just check for Open returning None
        try:
            ogr_dataset = ogr.Open(file_path)
        except Exception as e:
            logger.debug("Could not open dataset using ogr.")
            logger.debug(e)
        finally:
            if not ogr_dataset:
                logger.debug("Unknown file format: {0}".format(file_path))
            else:
                logger.info(f"The dataset: {file_path} opened with ogr.")
            return ogr_dataset or gdal_dataset
    except RuntimeError as ex:
        if ("not recognized as a supported file format" not in str(ex)) or (
            "Error browsing database for PostGIS Raster tables" in str(ex)
        ):
            raise ex
    finally:
        cleanup_dataset(gdal_dataset)
        cleanup_dataset(ogr_dataset)


def cleanup_dataset(dataset):
    """
    Given an input gdal.Dataset or ogr.DataSource, destroy it.
    NB: referring to this object's members after destruction will crash the Python interpreter.
    :param resources: Dataset / DataSource to destroy
    """
    if dataset:
        logger.info("Closing the resources: {}.".format(dataset))
        # https://trac.osgeo.org/gdal/wiki/PythonGotchas#CertainobjectscontainaDestroymethodbutyoushouldneveruseit
        del dataset


@retry
def get_meta(ds_path, is_raster=True):
    """
    This function is a wrapper for the get_gdal metadata because if there is a database diconnection there is no obvious
    way to clean up and free those resources therefore it is put on a separate process and if it fails it can just be
    tried again.

    This is using GDAL 2.2.4 this should be checked again to see if it can be simplified in a later version.
    :param ds_path: String: Path to dataset
    :param is_raster Boolean: Do not try to do OGR lookup if a raster dataset can be opened, otherwise it will try both,
         and return the vector if that is an option.
    :return: Metadata dict
        driver: Short name of GDAL driver for dataset
        is_raster: True if dataset is a raster type
        nodata: NODATA value for all bands if all bands have the same one, otherwise None (raster sets only)
    """

    multiprocess_queue = multiprocessing.dummy.Queue()
    proc = multiprocessing.dummy.Process(
        target=get_gdal_metadata,
        args=(
            ds_path,
            is_raster,
            multiprocess_queue,
        ),
    )
    proc.start()
    proc.join()
    return multiprocess_queue.get()


def get_gdal_metadata(ds_path, is_raster, multiprocess_queue):
    """
    Don't call this directly use get_meta.

    Given a path to a raster or vector dataset, return the appropriate driver type.

    :param ds_path: String: Path to dataset
    :param A multiprocess queue.
    :return: None.
    """

    dataset = None
    ret = {"driver": None, "is_raster": None, "nodata": None}

    try:
        dataset = open_dataset(ds_path, is_raster)
        if isinstance(dataset, ogr.DataSource):
            ret["driver"] = dataset.GetDriver().GetName()
            ret["is_raster"] = False

        elif isinstance(dataset, gdal.Dataset):
            ret["driver"] = dataset.GetDriver().ShortName
            ret["is_raster"] = True
            if dataset.RasterCount:
                bands = list(set([dataset.GetRasterBand(i + 1).GetNoDataValue() for i in range(dataset.RasterCount)]))
                if len(bands) == 1:
                    ret["nodata"] = bands[0]

        if ret["driver"]:
            logger.debug("Identified dataset {0} as {1}".format(ds_path, ret["driver"]))
        else:
            logger.debug("Could not identify dataset {0}".format(ds_path))

        multiprocess_queue.put(ret)
    finally:
        cleanup_dataset(dataset)


def get_area(geojson):
    """
    Given a GeoJSON string or object, return an approximation of its geodesic area in km².

    The geometry must contain a single polygon with a single ring, no holes.
    Based on Chamberlain and Duquette's algorithm: https://trs.jpl.nasa.gov/bitstream/handle/2014/41271/07-0286.pdf
    :param geojson: GeoJSON selection area
    :return: area of geojson ring in square kilometers
    """
    earth_r = 6371  # km

    def rad(d):
        return math.pi * d / 180

    if isinstance(geojson, str):
        geojson = json.loads(geojson)

    if hasattr(geojson, "geometry"):
        geojson = geojson["geometry"]

    geom_type = geojson["type"].lower()
    if geom_type == "polygon":
        polys = [geojson["coordinates"]]
    elif geom_type == "multipolygon":
        polys = geojson["coordinates"]
    else:
        return RuntimeError("Invalid geometry type: %s" % geom_type)

    a = 0
    for poly in polys:
        ring = poly[0]
        if len(ring) < 4:
            continue
        ring.append(ring[-2])  # convenient for circular indexing
        for i in range(len(ring) - 2):
            a += (rad(ring[i + 1][0]) - rad(ring[i - 1][0])) * math.sin(rad(ring[i][1]))

    area = abs(a * (earth_r ** 2) / 2)
    return area


def is_envelope(geojson_path):
    """
    Given a path to a GeoJSON file, reads it and determines whether its coordinates correspond to a WGS84 bounding box,
    i.e. lat1=lat2, lon2=lon3, lat3=lat4, lon4=lon1, to tell whether there's need for an alpha layer in the output
    :param geojson_path: Path to GeoJSON selection file
    :return: True if the given geojson is an envelope/bounding box, with one polygon and one ring.
    """
    try:
        if not os.path.isfile(geojson_path) and isinstance(geojson_path, str):
            geojson = json.loads(geojson_path)
        else:
            with open(geojson_path, "r") as gf:
                geojson = json.load(gf)

        geom_type = geojson["type"].lower()
        if geom_type == "polygon":
            polys = [geojson["coordinates"]]
        elif geom_type == "multipolygon":
            polys = geojson["coordinates"]
        else:
            return False  # Points/lines aren't envelopes

        if len(polys) != 1:
            return False  # Multipolygons aren't envelopes

        poly = polys[0]
        if len(poly) != 1:
            return False  # Polygons with multiple rings aren't envelopes

        ring = poly[0]
        if len(ring) != 5 or ring[4] != ring[0]:
            return False  # Envelopes need exactly four valid coordinates

        # Envelopes will have exactly two unique coordinates, for both x and y, out of those four
        ret = len(set([coord[0] for coord in ring])) == len(set([coord[1] for coord in ring])) == 2
        return ret

    except (IndexError, IOError, ValueError):
        # Unparseable JSON or unreadable file: play it safe
        return False


@retry
def convert(
    boundary=None,
    input_file=None,
    output_file=None,
    src_srs=4326,
    driver=None,
    layers=None,
    layer_name=None,
    task_uid=None,
    projection: int = 4326,
    creation_options: list = None,
    dataset_creation_options: list = None,
    layer_creation_options: list = None,
    is_raster: bool = True,
    warp_params: dict = None,
    translate_params: dict = None,
    use_translate: bool = False,
    access_mode: str = "overwrite",
    config_options: List[Tuple[str]] = None,
    distinct_field=None,
):
    """
    Uses gdal to convert and clip a supported dataset file to a mask if boundary is passed in.
    :param use_translate: A flag to force the use of translate instead of warp.
    :param layer_creation_options: Data options specific to vector conversion.
    :param dataset_creation_options: Data options specific to vector conversion.
    :param translate_params: A dict of params to pass into gdal translate.
    :param warp_params: A dict of params to pass into gdal warp.
    :param is_raster: A explicit declaration that dataset is raster (for disambiguating mixed mode files...gpkg)
    :param boundary: A geojson file or bbox (xmin, ymin, xmax, ymax) to serve as a cutline
    :param input_file: A raster or vector file to be clipped
    :param output_file: The dataset to put the clipped output in (if not specified will use in_dataset)
    :param driver: Short name of output driver to use (defaults to input format)
    :param layer_name: Table name in database for in_dataset
    :param layers: A list of layers to include for translation.
    :param task_uid: A task uid to update
    :param projection: A projection as an int referencing an EPSG code (e.g. 4326 = EPSG:4326)
    :param creation_options: Additional options to pass to the convert method (e.g. "-co SOMETHING")
    :param config_options: A list of gdal configuration options as a tuple (option, value).
    :return: Filename of clipped dataset
    """

    if isinstance(input_file, str) and not use_translate:
        input_file = [input_file]

    meta_list = []
    for _index, _file in enumerate(input_file):
        input_file[_index], output_file = get_dataset_names(_file, output_file)
        meta_list.append(get_meta(input_file[_index], is_raster))

    src_src = f"EPSG:{src_srs}"
    dst_src = f"EPSG:{projection}"
    # Currently, when there are more than 1 files, they much each be the same driver, making the meta the same.
    meta = meta_list[0]
    if not driver:
        driver = meta["driver"] or "gpkg"

    # Geopackage raster only supports byte band type, so check for that
    band_type = None
    dstalpha = None
    if driver.lower() == "gpkg":
        band_type = gdal.GDT_Byte
    if meta.get("nodata") is None and meta.get("is_raster"):
        dstalpha = True

    # Clip the dataset if a boundary is passed in.
    temp_boundfile = None
    geojson = None
    bbox = None
    if boundary:
        # Strings are expected to be a file.
        if isinstance(boundary, str):
            if not os.path.isfile(boundary):
                raise Exception(f"Called convert using a boundary of {boundary} but no such path exists.")
        elif is_valid_bbox(boundary):
            geojson = GeocodeAdapter.bbox2polygon(boundary)
            bbox = boundary
        elif isinstance(boundary, dict):
            geojson = boundary
        if geojson:
            temp_boundfile = NamedTemporaryFile(suffix=".json")
            temp_boundfile.write(json.dumps(geojson).encode())
            temp_boundfile.flush()
            boundary = temp_boundfile.name

    if meta["is_raster"]:
        cmd = get_task_command(
            convert_raster,
            input_file,
            output_file,
            driver=driver,
            creation_options=creation_options,
            band_type=band_type,
            dst_alpha=dstalpha,
            boundary=boundary,
            src_srs=src_src,
            dst_srs=dst_src,
            task_uid=task_uid,
            warp_params=warp_params,
            translate_params=translate_params,
            use_translate=use_translate,
            config_options=config_options,
        )
    else:
        cmd = get_task_command(
            convert_vector,
            input_file,
            output_file,
            driver=driver,
            dataset_creation_options=dataset_creation_options,
            layer_creation_options=layer_creation_options,
            src_srs=src_src,
            dst_srs=dst_src,
            layers=layers,
            layer_name=layer_name,
            task_uid=task_uid,
            boundary=boundary,
            bbox=bbox,
            access_mode=access_mode,
            config_options=config_options,
            distinct_field=distinct_field,
        )
    try:
        task_process = TaskProcess(task_uid=task_uid)
        task_process.start_process(cmd)
    except Exception as e:
        logger.error(e)
        raise Exception("File conversion failed.  Please try again or contact support.")

    finally:
        if temp_boundfile:
            temp_boundfile.close()

    if requires_zip(driver):
        logger.debug(f"Requires zip: {output_file}")
        output_file = create_zip_file(output_file, get_zip_name(output_file))

    return output_file


def get_task_command(function, *args, **kwargs):
    return lambda: function(*args, **kwargs)


def get_dataset_names(input_file, output_file):
    """
    This is a helper that will get us the name of the output_dataset.
    :param input_file: The name of the dataset to convert.
    :param output_file: (Optional) The path to convert the file.
    :return: An output dataset name.
    """
    if not input_file:
        raise Exception("Not provided: 'in' dataset")

    # Strip optional file prefixes
    file_prefix, in_dataset_file = strip_prefixes(input_file)
    if not output_file:
        output_file = in_dataset_file

    # don't operate on the original file.  If the renamed file already exists,
    # then don't try to rename, since that file may not exist if this is a retry.
    if output_file == in_dataset_file:
        in_dataset_file = rename_duplicate(in_dataset_file)
        input_file = f"{file_prefix}{in_dataset_file}"
    return input_file, output_file


def clean_options(options):
    return {option: value for option, value in options.items() if value is not None}


def convert_raster(
    input_files,
    output_file,
    driver=None,
    access_mode="overwrite",
    creation_options=None,
    band_type=None,
    dst_alpha=None,
    boundary=None,
    src_srs=None,
    dst_srs=None,
    task_uid=None,
    warp_params: dict = None,
    translate_params: dict = None,
    use_translate: bool = False,
    config_options: List[Tuple[str]] = None,
):
    """
    :param warp_params: A dict of options to pass to gdal warp (done first in conversion), overrides other settings.
    :param translate_params: A dict of options to pass to gdal translate (done second in conversion),
        overrides other settings.
    :param input_files: A file or list of files to convert.
    :param output_file: The file to convert.
    :param driver: The file format to convert.
    :param creation_options: Special GDAL options for conversion.
        Search for "gdal driver <format> creation options" creation options for driver specific implementation.
    :param band_type: The GDAL data type (e.g. gdal.GDT_BYTE).
    :param dst_alpha: If including an alpha band in the destination file.
    :param boundary: The boundary to be used for clipping, this must be a file.
    :param src_srs: The srs of the source (e.g. "EPSG:4326")
    :param dst_srs: The srs of the destination (e.g. "EPSG:3857")
    :param task_uid: The eventkit task uid used for tracking the work.
    :param use_translate: Make true if needing to use translate for conversion instead of warp.
    :param config_options: A list of gdal configuration options as a tuple (option, value).
    :return: The output file.
    """
    if not driver:
        raise Exception("Cannot use convert_raster without specififying a gdal driver.")

    if isinstance(input_files, str) and not use_translate:
        input_files = [input_files]
    elif isinstance(input_files, list) and use_translate:
        # If a single file is provided in an array, we can simply pull it out
        if len(input_files) == 1:
            input_files = input_files[0]
        else:
            raise Exception("Cannot use_translate with a list of files.")
    gdal.UseExceptions()
    subtask_percentage = 50 if driver.lower() == "gtiff" else 100
    options = clean_options(
        {
            "callback": progress_callback,
            "callback_data": {"task_uid": task_uid, "subtask_percentage": subtask_percentage},
            "creationOptions": creation_options,
            "format": driver,
        }
    )
    if not warp_params:
        warp_params = clean_options(
            {"outputType": band_type, "dstAlpha": dst_alpha, "srcSRS": src_srs, "dstSRS": dst_srs}
        )
    if not translate_params:
        translate_params = dict()
    if boundary:
        warp_params.update({"cutlineDSName": boundary, "cropToCutline": True})
    # Keep the name imagery which is used when seeding the geopackages.
    # Needed because arcpy can't change table names.
    if driver.lower() == "gpkg":
        options["creationOptions"] = options.get("creationOptions", []) + ["RASTER_TABLE=imagery"]

    if use_translate:
        logger.info(
            f"calling gdal.Translate('{output_file}', {input_files}'),"
            f"{stringify_params(options)}, {stringify_params(warp_params)},)"
        )
        options.update(translate_params)
        gdal.Translate(output_file, input_files, **options)
    else:
        logger.info(
            f"calling gdal.Warp('{output_file}', [{', '.join(input_files)}],"
            f"{stringify_params(options)}, {stringify_params(warp_params)},)"
        )
        gdal.Warp(output_file, input_files, **options, **warp_params)

    if driver.lower() == "gtiff" or translate_params:
        # No need to compress in memory objects as they will be removed later.
        if "vsimem" in output_file:
            return output_file
        input_file, output_file = get_dataset_names(output_file, output_file)
        if translate_params:
            options.update(translate_params)
        else:
            options.update({"creationOptions": ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=YES"]})

        logger.info(f"calling gdal.Translate('{output_file}', '{input_file}', " f"{stringify_params(options)},)")
        gdal.Translate(output_file, input_file, **options)
    return output_file


def convert_vector(
    input_file,
    output_file,
    driver=None,
    access_mode="overwrite",
    src_srs=None,
    dst_srs=None,
    task_uid=None,
    layers=None,
    layer_name=None,
    boundary=None,
    bbox=None,
    dataset_creation_options=None,
    layer_creation_options=None,
    config_options: List[Tuple[str]] = None,
    distinct_field=None,
):
    """
    :param input_files: A file or list of files to convert.
    :param output_file: The file to convert.
    :param driver: The file format to convert.
    :param creation_options: Special GDAL options for conversion.
        Search for "gdal driver <format> creation options" creation options for driver specific implementation.
    :param access_mode: The access mode for the file (e.g. "append" or "overwrite")
    :param bbox: A bounding box as a list (w,s,e,n) to be used for limiting the AOI that is used during conversion.
    :param boundary: The boundary to be used for clipping.
        This must be a file (i.e. a path as a string) and cannot be used with bbox.
    :param src_srs: The srs of the source (e.g. "EPSG:4326")
    :param dst_srs: The srs of the destination (e.g. "EPSG:3857")
    :param task_uid: The eventkit task uid used for tracking the work.
    :param layers: A list of layers to include for translation.
    :param layer_name: Table name in database for in_dataset
    :param config_options: A list of gdal configuration options as a tuple (option, value).
    :param distinct_field: A field for selecting distinct features to prevent duplicates.
    :return: The output file.
    """
    if isinstance(input_file, str) and access_mode == "append":
        input_file = [input_file]
    elif isinstance(input_file, list) and access_mode == "overwrite":
        # If a single file is provided in an array, we can simply pull it out
        if len(input_file) == 1:
            input_file = input_file[0]
        else:
            raise Exception("Cannot overwrite with a list of files.")
    gdal.UseExceptions()
    options = clean_options(
        {
            "callback": progress_callback,
            "callback_data": {"task_uid": task_uid},
            "datasetCreationOptions": dataset_creation_options,
            "layerCreationOptions": layer_creation_options,
            "format": driver,
            "layers": layers,
            "layerName": layer_name,
            "srcSRS": src_srs,
            "dstSRS": dst_srs,
            "accessMode": access_mode,
            "reproject": src_srs != dst_srs,
            "skipFailures": True,
            "spatFilter": bbox,
            "options": ["-clipSrc", boundary] if boundary and not bbox else None,
        }
    )
    if "gpkg" in driver.lower():
        options["geometryType"] = ["PROMOTE_TO_MULTI"]
    if config_options:
        for config_option in config_options:
            gdal.SetConfigOption(*config_option)
    if access_mode == "append":
        for _input_file in input_file:
            logger.info(f"calling gdal.VectorTranslate('{output_file}', '{_input_file}', {stringify_params(options)})")
            gdal.VectorTranslate(output_file, _input_file, **options)
    else:
        gdal.VectorTranslate(output_file, input_file, **options)

    if distinct_field:
        logger.error(f"Normalizing features based on field: {distinct_field}")
        table_name = layer_name or os.path.splitext(os.path.basename(output_file))[0]
        options["SQLStatement"] = f"SELECT * from '{table_name}' GROUP BY '{distinct_field}'"
        options["SQLDialect"] = "sqlite"
        logger.error(f"calling gdal.VectorTranslate('{output_file}', '{output_file}', {stringify_params(options)})")
        gdal.VectorTranslate(output_file, rename_duplicate(output_file), **options)

    return output_file


def polygonize(input_file: str, output_file: str, output_type: str = "GeoJSON", band: int = None):
    """
    Polygonization groups similar pixel values into bins and draws a boundary around them.
    This is often used as a way to display raster information in a vector format. That can still be done here,
    but if a band isn't provided the function will try to guess at the mask band and will use that as both the
    converted layer and the mask.  The result should be a polygon of anywhere there are not black or not transparent
    pixels.

    :param input_file: The raster file to use to polygonize.
    :param output_file: The vector output file for the new data.
    :param output_type: The file type for output data (should be a vector type).
    :param band: The band to use for polygonization.
    :return:
    """

    src_ds = gdal.Open(input_file)

    if src_ds is None:
        logger.error("Unable to open source.")
        raise Exception("Failed to open the file.")

    try:
        band_index = band
        if not band_index:
            if src_ds.RasterCount == 4:
                band_index = 4
            elif src_ds.RasterCount == 3:
                # Likely RGB (jpg) add a transparency mask and use that.

                # Clean up pixel values of 1 0 0 or 0 0 1 caused by interleaving.
                nb_file = "/vsimem/nb"
                gdal.Nearblack(nb_file, input_file)

                # Convert to geotiff so that we can remove black pixels and use alpha mask for the polygon.
                tmp_file = "/vsimem/tmp.tif"
                convert_raster(nb_file, tmp_file, driver="gtiff", warp_params={"dstAlpha": True, "srcNodata": "0 0 0"})

                del nb_file
                src_ds = gdal.Open(tmp_file)
                band_index = 4
            elif src_ds.RasterCount == 2:
                band_index = 2
            else:
                band_index = 1
        mask_band = src_ds.GetRasterBand(band_index)
    except RuntimeError as e:
        logger.error(e)
        raise Exception("Unable to get raster band.")

    drv = ogr.GetDriverByName(output_type)
    dst_ds = drv.CreateDataSource(output_file)
    dst_layer = dst_ds.CreateLayer(output_file)

    # Use the mask band for both the polygonization and as a mask.
    gdal.Polygonize(mask_band, mask_band, dst_layer, -1, [])
    # Close files to read later.
    del dst_ds
    del src_ds

    return output_file


def stringify_params(params):
    return ", ".join([f"{k}='{v}'" for k, v in params.items()])


def get_dimensions(bbox: List[float], scale: int) -> (int, int):
    """
    :param bbox: A list [w, s, e, n].
    :param scale: A scale in meters per pixel.
    :return: A list [width, height] representing pixels
    """
    # Request at least one pixel
    width = get_distance([bbox[0], bbox[1]], [bbox[2], bbox[1]])
    height = get_distance([bbox[0], bbox[1]], [bbox[0], bbox[3]])

    scaled_width = int(width / scale) or 1
    scaled_height = int(height / scale) or 1
    return scaled_width, scaled_height


def get_line(coordinates):
    """

    :param coordinates: A list representing a single coordinate in decimal degrees.
        Example: [[W/E, N/S], [W/E, N/S]]
    :return: AN OGR geometry point.
    """
    # This line will implicitly be in EPSG:4326 because that is what the geojson standard specifies.
    geojson = json.dumps({"type": "LineString", "coordinates": coordinates})
    return ogr.CreateGeometryFromJson(geojson)


def get_distance(point_a, point_b):
    """
    Takes two points, and converts them to a line, converts the geometry to mercator and returns length in meters.
    The geometry is converted to mercator because length is based on the SRS unit of measure (meters for mercator).
    :param point_a: A list representing a single point [W/E, N/S].
    :param point_b: A list representing a single point [W/E, N/S].
    :return: Distance in meters.
    """
    line = get_line([point_a, point_b])
    reproject_geometry(line, 4326, 3857)
    return line.Length()


def reproject_geometry(geometry, from_srs, to_srs):
    """

    :param geometry: Converts an ogr geometry from one spatial reference system to another
    :param from_srs:
    :param to_srs:
    :return:
    """
    return geometry.Transform(get_transform(from_srs, to_srs))


def get_transform(from_srs, to_srs):
    """
    :param from_srs: A spatial reference (EPSG) represented as an int (i.e. EPSG:4326 = 4326)
    :param to_srs: A spatial reference (EPSG) represented as an int (i.e. EPSG:4326 = 4326)
    :return: An osr coordinate transformation object.
    """
    osr_axis_mapping_strategy = osr.OAMS_TRADITIONAL_GIS_ORDER
    source = osr.SpatialReference()
    source.ImportFromEPSG(from_srs)
    source.SetAxisMappingStrategy(osr_axis_mapping_strategy)
    target = osr.SpatialReference()
    target.ImportFromEPSG(to_srs)
    target.SetAxisMappingStrategy(osr_axis_mapping_strategy)

    return osr.CoordinateTransformation(source, target)


def merge_geotiffs(in_files, out_file, task_uid=None):
    """
    :param in_files: A list of geotiffs.
    :param out_file: A location for the result of the merge.
    :param task_uid: A task uid to track the conversion.
    :return: The out_file path.
    """
    cmd = get_task_command(convert_raster, in_files, out_file, task_uid=task_uid, driver="gtiff")

    try:
        task_process = TaskProcess(task_uid=task_uid)
        task_process.start_process(cmd)
    except Exception as e:
        logger.error(e)
        raise Exception("GeoTIFF merge process failed.")

    return out_file


def merge_geojson(in_files, out_file):
    """
    :param in_files: A list of geojson files.
    :param out_file: A location for the result of the merge.
    :param task_uid: A task uid to track the conversion.
    :return: The out_file path.
    """
    try:
        out_driver = ogr.GetDriverByName("GeoJSON")
        out_ds = out_driver.CreateDataSource(out_file)
        out_layer = out_ds.CreateLayer(out_file)

        for file in in_files:
            ds = ogr.Open(file)
            lyr = ds.GetLayer()
            for feat in lyr:
                out_feat = ogr.Feature(out_layer.GetLayerDefn())
                out_feat.SetGeometry(feat.GetGeometryRef().Clone())
                out_layer.CreateFeature(out_feat)
                out_feat = None  # NOQA
                out_layer.SyncToDisk()
        out_ds = None  # NOQA
    except Exception as e:
        logger.error(e)
        raise Exception("File merge process failed.")

    return out_file


def get_band_statistics(file_path, band=1):
    """
    Returns the band statistics for a specific raster file and band
    :param file_path: The path to the file.
    :param band: A specific raster band (defaults to 1).
    :return: A list [min, max, mean, std_dev]
    """
    image_file = None
    try:
        gdal.UseExceptions()
        image_file = gdal.Open(file_path)
        raster_band = image_file.GetRasterBand(band)
        return raster_band.GetStatistics(False, True)
    except Exception as e:
        logger.error(e)
        logger.error("Could not get statistics for {0}:{1}".format(file_path, raster_band))
        return None
    finally:
        # Need to close the dataset.
        cleanup_dataset(image_file)  # NOQA


def rename_duplicate(original_file: str) -> str:

    # Some files we may not want to rename or overwrite.  For example if PBF is used for source data, we don't want to
    # create duplicates of it and the gdal driver doesn't support writing PBF anyway, so this is likely a mistake.
    protected_files = [".pbf"]
    if os.path.splitext(original_file)[1] in protected_files:
        raise Exception(f"The {original_file} cannot be renamed it is protected and/or not writable by this module.")
    returned_file = os.path.join(
        os.path.dirname(original_file),
        "old_{0}".format(os.path.basename(original_file)),
    )
    # if the original and renamed files both exist, we can remove the renamed version, and then rename the file.
    if os.path.isfile(returned_file) and os.path.isfile(original_file):
        os.remove(returned_file)
    # If the original file doesn't exist but the renamed version does, then something failed after a rename, and
    # this is now retrying the operation.
    if not os.path.isfile(returned_file):
        logger.info("Renaming '{}' to '{}'".format(original_file, returned_file))
        os.rename(original_file, returned_file)
    return returned_file


def strip_prefixes(dataset: str) -> (str, str):
    prefixes = ["GTIFF_RAW:"]
    removed_prefix = ""
    output_dataset = dataset
    for prefix in prefixes:
        cleaned_dataset = output_dataset.lstrip(prefix)
        if cleaned_dataset != output_dataset:
            removed_prefix = prefix
        output_dataset = cleaned_dataset
    return removed_prefix, output_dataset


def get_chunked_bbox(bbox, size: tuple = None, level: int = None):
    """
    Chunks a bbox into a grid of sub-bboxes.
    :param bbox: bbox in 4326, representing the area of the world to be chunked
    :param size: optional image size to use when calculating the resolution.
    :param level:  The level to use for the affected level.
    :return: enclosing bbox of the area, dimensions of the grid, bboxes of all tiles.
    """
    from eventkit_cloud.utils.image_snapshot import get_resolution_for_extent

    # Calculate the starting res for our custom grid
    # This is the same method we used when taking snap shots for data packs
    resolution = get_resolution_for_extent(bbox, size)
    # Make a subgrid of 4326 that spans the extent of the provided bbox
    # min res specifies the starting zoom level
    mapproxy_grid = tile_grid(srs=4326, bbox=bbox, bbox_srs=4326, origin="ul", min_res=resolution)
    # bbox is the bounding box of all tiles affected at the given level, unused here
    # size is the x, y dimensions of the grid
    # tiles at level is a generator that returns the tiles in order
    tiles_at_level = mapproxy_grid.get_affected_level_tiles(bbox, 0)[2]
    # convert the tiles to bboxes representing the tiles on the map
    return [mapproxy_grid.tile_bbox(_tile) for _tile in tiles_at_level]
