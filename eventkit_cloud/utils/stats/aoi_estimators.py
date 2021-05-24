import logging
import eventkit_cloud.utils.stats.generator as ek_stats

from django.core.exceptions import ObjectDoesNotExist

from mapproxy import grid as mapproxy_grid
from mapproxy import srs as mapproxy_srs

from eventkit_cloud.jobs.models import DataProvider
from eventkit_cloud.utils.stats.geomutils import get_area_bbox

from enum import Enum

logger = logging.getLogger(__name__)


class Stats(object):
    """
    Util classes acting as a container for statistics constants.

    This may not be an exhaustive list, it's just what we have used so far in practice or testing.
    """

    # Average result, tends to give the best result while not underestimating
    MEAN = "mean"
    CI_99 = "ci_99"
    CI_95 = "ci_95"
    CI_90 = "ci_90"
    MAX = "max"
    MIN = "min"

    class Fields(object):
        """Container class representing methods used for statistics."""

        # Used for raster images, megabytes per pixel
        MPP = "mpp"
        # Used for vector size estimates, size per km (size of result / area of result in km)
        SIZE = "size"
        # Used for duration estimates for all types, seconds per unit area
        DURATION = "duration"
        # Unsure what the intention for this is, currently unused but exists.
        AREA = "area"


class AoiEstimator(object):
    """Object used to return estimates for a selected region (AOI/BBOX)"""

    class Types(Enum):
        """Simple enum representing available estimate types."""

        # First two are the same, time and duration alias each other.
        TIME = "time"
        DURATION = "duration"
        SIZE = "size"

        @classmethod
        def is_valid(cls, value):
            """
            Return True if the specified value is valid for this enum.

            Checks if the value is a valid estimate that we provide. First checks if it is a valid enum type,
            this is the builtin way to check if a value is part of the enum. Then we fall back and check for string
            type access to support specifying the estimate type with strings.
            :param value:
            :return:
            """
            if isinstance(value, AoiEstimator.Types):
                return True
            try:
                return any(value.lower() == _item.value.lower() for _item in cls)
            except AttributeError:
                return False

    def __init__(
        self,
        bbox,
        bbox_srs="4326",
        with_clipping=True,
        cap_estimates=True,
        min_zoom=None,
        max_zoom=None,
    ):
        # It would be good to integrate a BBOX class to pass around instead of doing this
        # It can get cumbersome and lead to errors when the bbox srs is assumed
        self.bbox = bbox
        self.bbox_srs = bbox_srs
        self.min_zoom = min_zoom
        self.max_zoom = max_zoom
        self._with_clipping = with_clipping
        self._cap_estimates = cap_estimates
        self._results = dict()

    def get_estimate_from_slug(self, estimate_type, provider_slug):
        """Get the specified estimate type for a provider by doing a slug lookup."""
        try:
            provider = DataProvider.objects.select_related("export_provider_type").get(slug=provider_slug)
        except ObjectDoesNotExist:
            raise ValueError("Provider slug '{}' is not valid".format(provider_slug))
        return self.get_estimate(estimate_type, provider)

    def get_estimates(self, estimate_type, providers):
        """
        Get a dictionary mapping providers to their estimates.

        :param estimate_type: string identifier for the type of estimate ('duration', 'size')
        :param providers: flat list of
        :return:
        """
        return {_provider: self.get_estimate(estimate_type, _provider) for _provider in providers}

    def get_estimate(self, estimate_type, provider):
        if not self.Types.is_valid(estimate_type):
            raise ValueError(f"""'{estimate_type}' is not a valid estimate type.""")
        if estimate_type == self.Types.SIZE:
            return self._get_size_estimate(provider)
        elif estimate_type == self.Types.TIME or estimate_type == self.Types.DURATION:
            return self._get_time_estimate(provider)
        # This ideally should never be reached, it can only be reached when calling this directly.
        raise ValueError(f"""Unable to compute '{estimate_type}' estimate.""")

    def _get_size_estimate(self, provider):
        """Get size estimate for this provider by checking the provider type."""
        if is_raster_single(provider) or is_raster_tile_grid(provider):
            return get_raster_tile_grid_size_estimate(
                provider,
                self.bbox,
                self.bbox_srs,
                with_clipping=self._with_clipping,
                min_zoom=self.min_zoom,
                max_zoom=self.max_zoom,
            )
        elif is_vector(provider):
            return get_vector_estimate(provider, bbox=self.bbox, srs=self.bbox_srs)
        else:
            logger.info(f"""Non-specific provider found with slug {provider.slug}, falling back to vector estimate""")
            return get_vector_estimate(provider, bbox=self.bbox, srs=self.bbox_srs)

    def _get_time_estimate(self, provider):
        """Get the time estimate for the specified provider."""
        return get_time_estimate(provider, bbox=self.bbox, bbox_srs=self.bbox_srs)


def get_size_estimate_slug(slug, bbox, srs="4326", min_zoom=None, max_zoom=None):
    """
    See get_size_estimate
    :param slug: slug of the DataProvider
    :param bbox
    :param srs
    """
    try:
        provider = DataProvider.objects.select_related("export_provider_type").get(slug=slug)
    except ObjectDoesNotExist:
        raise ValueError("Provider slug '{}' is not valid".format(slug))

    return get_size_estimate(provider, bbox, srs, min_zoom, max_zoom)


def get_size_estimate(provider, bbox, srs="4326", min_zoom=None, max_zoom=None):
    """
    The estimate size of a data export from provider over the specified region, in MBs
    :param provider: DataProvider
    :param bbox: Bounding box defining the area to estimate
    :param srs: The srs of the bounding box
    :return: (estimate_size_mb, metadata)
    """
    if is_raster_tile_grid(provider):
        return get_raster_tile_grid_size_estimate(provider, bbox, srs)
    elif is_raster_single(provider):
        return get_raster_tile_grid_size_estimate(provider, bbox, srs)
    elif is_vector(provider):
        return get_vector_estimate(provider, bbox, srs)
    else:
        # Might have statistics on this type, or it will revert to GLOBAL
        return get_vector_estimate(provider, bbox, srs)


def is_raster_tile_grid(provider):
    """
    :param provider: The DataProvider to test
    :return: True if the DataProvider exports a tilegrid of raster data
    """
    type_name = provider.export_provider_type.type_name
    return type_name == "wms" or type_name == "wmts" or type_name == "tms"


def is_raster_single(provider):
    """
    :param provider: The DataProvider to test
    :return: True if the DataProvider exports a single raster file
    """
    type_name = provider.export_provider_type.type_name
    return type_name == "wcs"


def is_vector(provider):
    """
    :param provider: The DataProvider to test
    :return: True if the DataProvider exports vector data (usually as geopackage)
    """
    type_name = provider.export_provider_type.type_name
    return type_name == "osm-generic" or type_name == "osm" or type_name == "wfs"


def get_raster_tile_grid_size_estimate(provider, bbox, srs="4326", with_clipping=True, min_zoom=None, max_zoom=None):
    """
    :param provider: The DataProvider to test
    :param bbox: The bounding box of the request
    :param srs: The SRS of the bounding box
    :param with_clipping: see get_total_num_pixels
    :return: (estimate in mbs, object w/ metadata about how it was generated)
    """
    # TODO: Both total_pixels and query intersect the tile grid, can save time if we do it once for both
    tile_grid = ek_stats.get_provider_grid(provider, min_zoom, max_zoom)
    total_pixels = ek_stats.get_total_num_pixels(tile_grid, bbox, srs, with_clipping)

    mpp, method = ek_stats.query(
        provider.slug,
        field=Stats.Fields.MPP,
        statistic_name=Stats.MEAN,
        bbox=bbox,
        bbox_srs=srs,
        gap_fill_thresh=0.1,
        default_value=0.00000006,
    )
    method["mpp"] = mpp
    method["with_clipping"] = with_clipping
    # max acceptable is expected maximum number of bytes for the specified amount of pixels
    # pixels * <pixels per byte> / <bytes per MB>
    max_acceptable = total_pixels * 3 / 1e6
    estimate = total_pixels * mpp
    return estimate if estimate < max_acceptable else max_acceptable, method


def get_vector_estimate(provider, bbox, srs="4326"):
    """
    :param provider: The DataProvider to test
    :param bbox: The bounding box of the request
    :param srs: The SRS of the bounding box
    :return: (estimate in mbs, object w/ metadata about how it was generated)
    """
    # TODO tile_grid params should be serialized on all_stats object
    tile_grid = ek_stats.get_default_tile_grid()
    req_bbox = mapproxy_grid.grid_bbox(bbox, mapproxy_srs.SRS(srs), tile_grid.srs)
    req_area = ek_stats.get_area_bbox(req_bbox)

    # Compute estimate
    size_per_km, method = ek_stats.query(
        provider.slug,
        field=Stats.Fields.SIZE,
        statistic_name=Stats.MEAN,
        bbox=bbox,
        bbox_srs=srs,
        gap_fill_thresh=0.1,
        default_value=0,
    )
    method["size_per_km"] = size_per_km
    return req_area * size_per_km, method


def get_time_estimate(provider, bbox, bbox_srs="4326"):
    """
    :param provider: The DataProvider to test
    :param bbox: The bounding box of the request
    :param bbox_srs: The SRS of the bounding box
    :return: (estimate in seconds, object w/ metadata about how it was generated)
    """
    duration_per_unit_area, method = ek_stats.query(
        provider.slug,
        field=Stats.Fields.DURATION,
        statistic_name=Stats.MEAN,
        bbox=bbox,
        bbox_srs=bbox_srs,
        gap_fill_thresh=0.1,
        default_value=0,
    )

    area = get_area_bbox(bbox)
    estimate = area * duration_per_unit_area

    # If the estimate is more than a day, return a day and one second, we will use this on the front end.
    max_acceptable = 60 * 60 * 24  # Hard capping time estimates to one day (in seconds)
    return estimate if estimate < max_acceptable else max_acceptable + 1, method
