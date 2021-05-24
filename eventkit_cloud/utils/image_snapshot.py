import os
import shutil
from requests import Response
from webtest.response import TestResponse
import logging
import copy

from PIL import Image
from io import BytesIO

from django.conf import settings
from eventkit_cloud.utils import s3
from eventkit_cloud.jobs.models import MapImageSnapshot
from eventkit_cloud.jobs.helpers import (
    get_provider_image_download_dir,
    get_provider_image_download_path,
)
from eventkit_cloud.tasks.export_tasks import make_dirs
from eventkit_cloud.utils.helpers import (
    get_download_paths,
    get_relative_path_from_staging,
)
from eventkit_cloud.utils.mapproxy import create_mapproxy_app
from urllib.parse import urlparse
from typing import Union

from mapproxy.grid import tile_grid

logger = logging.getLogger(__name__)

WGS84_FULL_WORLD = [-180, -90, 180, 90]


def get_wmts_snapshot_image(base_url: str, zoom_level: int = None, bbox: list = None):
    """
    Returns an image comprised of all tiles touched by bbox at a given zoom_level for the provided provider URL.

    The higher zoom_level is and the bigger the area encompossed by bbox, the longer this takes. At high zooms this
    can be prohibitively expensive, even if optimized. This function should be used to generate small images of
    small regions or high level (low zoom) snapshots of a map. Full world, zoom level 0 is used to generate Thumbnails.

    :param base_url: URL that tiles are to be requested from, must be formatted with {x], {y}, and {z}
    :param zoom_level: level to look for tiles at, will be determined automatically based on extent if one isn't passed.
    :param bbox: region of the world to get tiles for
    :return: A Pillow Image object built for the collected tiles.
    """
    if bbox is None:
        bbox = copy.copy(WGS84_FULL_WORLD)
    # Creates and returns a TileGrid object, allows us specify min_res instead of supplying the resolution list.
    min_res = 0.703125  # EPSG:4326 with two tiles at level 0
    mapproxy_grid = tile_grid(
        srs=4326,
        min_res=min_res,
        bbox_srs=4326,
        bbox=copy.copy(WGS84_FULL_WORLD),
        origin="ul",
    )

    if zoom_level is None:
        resolution = get_resolution_for_extent(bbox)
        zoom_level = mapproxy_grid.closest_level(resolution)

    tiles = mapproxy_grid.get_affected_level_tiles(bbox, zoom_level)
    dim_col, dim_row = tiles[1]
    # this will be a generator that returns all affected tile coords, scanning row by row (col increasing first)
    # convert to list for easy access
    tiles = [_tile_coords for _tile_coords in tiles[2]]

    # Grab a tile and read it to get the size we will be working with.
    # This is WMTS, all subsequent tiles will be the same size
    if getattr(settings, "SITE_NAME") in base_url:
        # If the request is local, use a mapproxy app here instead of making a network request to the view.
        parsed_url = urlparse(base_url)  # base_url = https://test/map/slug/one/two?q1=1&q2=2
        split_path = parsed_url.path.lstrip("/").split("/")  # ['map','slug','one','two']
        slug = split_path[1]
        map_path = split_path[2:]
        base_url = f"/{'/'.join(map_path)}?{parsed_url.query}"  # /one/two?q1=1&q2=2
        mapproxy_app = create_mapproxy_app(slug)
        requests = mapproxy_app
    else:
        # Ensure proper requests is loaded
        import requests
    response = requests.get(base_url.format(x=0, y=0, z=0))
    tile = get_tile(response)
    size_x, size_y = tile.size  # These should be the same

    # Create the image we will paste all other tiles into.
    snapshot = Image.new("RGB", (size_x * dim_col, size_y * dim_row))
    tile_count = 0
    for _row in range(dim_row):
        for _col in range(dim_col):
            # Capture the coords for this tile.
            tile_coords = tiles[tile_count]
            response = requests.get(base_url.format(x=tile_coords[0], y=tile_coords[1], z=zoom_level))
            tile = get_tile(response)
            # Paste this tile into the corresponding position relative to the overall image.
            # Tiles will inserted one after the other, left to right, top to bottom.
            snapshot.paste(im=tile, box=(size_x * _col, size_y * _row))
            tile_count += 1
    return snapshot


def get_tile(response: Union[Response, TestResponse]) -> Image:
    """
    A wrapper to get content from requests response, or webop response.
    :param response:
    :return:
    """
    content = getattr(response, "content", None) or getattr(response, "body", None)
    return Image.open(BytesIO(content))


def save_thumbnail(base_url, filepath):
    """
    Grab a high level snapshot of a map in EPSG:4326.

    :param base_url: A TMS style URL capable to be formatted with {x}, {y}, and {z}.
    :param filepath: name for the file to be saved as.
    :return: Full filepath on success
    """
    thumbnail_size = (90, 45)
    thumbnail = get_wmts_snapshot_image(base_url, zoom_level=0)

    full_filepath = f"{filepath}.jpg"
    thumbnail.thumbnail(thumbnail_size)
    thumbnail.save(full_filepath)
    return full_filepath


def fit_to_area(image, pixels_x=500, pixels_y=250):
    cur_size_x, cur_size_y = image.size
    if cur_size_x < cur_size_y:
        if pixels_x > pixels_y:
            _temp = pixels_y
            pixels_y = pixels_x
            pixels_x = _temp

    image.thumbnail(size=(pixels_x, pixels_y))
    return image


def make_thumbnail_downloadable(filepath, provider_uid, download_filename=None):
    filename = os.path.basename(filepath)
    if download_filename is None:
        download_filename = filename

    filesize = os.stat(filepath).st_size
    thumbnail_snapshot = MapImageSnapshot.objects.create(download_url="", filename=filepath, size=filesize)
    if getattr(settings, "USE_S3", False):
        download_url = s3.upload_to_s3(
            thumbnail_snapshot.uid,
            filepath,
            download_filename,
        )
        os.remove(filepath)
    else:
        download_path = os.path.join(get_provider_image_download_dir(provider_uid), download_filename)
        download_url = os.path.join(get_provider_image_download_path(provider_uid), download_filename)
        make_dirs(os.path.split(download_path)[0])
        shutil.copy(filepath, download_path)

    thumbnail_snapshot.download_url = download_url
    thumbnail_snapshot.save()

    return thumbnail_snapshot


def make_snapshot_downloadable(staging_filepath, relative_path=None, download_filename=None, copy=False):
    """
    Move an image from a staging location to a download location.

    :param staging_filepath: Where the image is initially situated.
    :param relative_path: Where the file should be moved to for download (ignored when USE_S3 is true)
    :param download_filename: optional rename of the original file.
    :param copy: if true the file will be copied instead of moved.
    :return:
    """
    filename = os.path.basename(staging_filepath)
    if download_filename is None:
        download_filename = filename

    filesize = os.stat(staging_filepath).st_size
    thumbnail_snapshot = MapImageSnapshot.objects.create(download_url="", filename=filename, size=filesize)
    if getattr(settings, "USE_S3", False):
        download_url = s3.upload_to_s3(staging_filepath, download_filename)
    else:
        if relative_path is None:
            relative_path = os.path.split(get_relative_path_from_staging(staging_filepath))[0]
        download_path, download_url = get_download_paths(relative_path)
        download_url = os.path.join(download_url, filename)
        make_dirs(download_path)
        # Source location (from) gets moved/copied to the destination (to)
        from_to = [staging_filepath, os.path.join(download_path, filename)]
        if copy:
            shutil.copy(*from_to)
        else:
            shutil.move(*from_to)

    thumbnail_snapshot.download_url = download_url
    thumbnail_snapshot.save()

    return thumbnail_snapshot


def get_resolution_for_extent(extent: list, size: tuple = None):
    """
    :param extent: A bounding box as a list [w,s,e,n].
    :param optional size: The pixel size of the image to get the resolution for as a tuple.
    :return: The resolution at which the extent will render at the given size.
    """
    if size is None:
        size = (1024, 512)

    size_x, size_y = size
    x_resolution = get_width(extent) / size_x
    y_resolution = get_height(extent) / size_y
    return max([x_resolution, y_resolution])


def get_width(extent: list):
    """
    :param extent: A bounding box as a list [w,s,e,n].
    :return: The width of the given extent.
    """
    return extent[2] - extent[0]


def get_height(extent: list):
    """
    :param extent: A bounding box as a list [w,s,e,n].
    :return: The height of the given extent.
    """
    return extent[3] - extent[1]
