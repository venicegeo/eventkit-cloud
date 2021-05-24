import copy
import datetime
import itertools
import json
import logging
import math
import os
import statistics
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import List

from django import db
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db.models.query import QuerySet
from mapproxy import grid as mapproxy_grid
from mapproxy import srs as mapproxy_srs

from eventkit_cloud.jobs.models import DataProvider
from eventkit_cloud.tasks.enumerations import TaskState
from eventkit_cloud.tasks.models import ExportTaskRecord, ExportRun, DataProviderTaskRecord
from eventkit_cloud.utils.client import parse_duration
from eventkit_cloud.utils.stats.geomutils import (
    get_area_bbox,
    get_bbox_intersect,
    get_geometry_description,
)

logger = logging.getLogger(__name__)
_dbg_geom_cache_misses = 0
MAX_SAMPLES_PER_TARGET = 2000
DEFAULT_CACHE_EXPIRATION = 60 * 60 * 24 * 7  # expire in a week
tid_cache_prefix = "generator.tidcache"
global_key = "GLOBAL"
# Method to pull normalized data values off of the run, provider_task, or provider_task.task objects


def get_statistics(provider_slug, force=os.getenv("FORCE_STATISTICS_RECOMPUTE", False)):
    """
    :param provider_slug: A slug value of the data provider you want to get statistics for.
    :param force: True to re-compute the desired statistics
    :return: The statistics object
    """
    cache_key = f"{provider_slug}_data_statistics"

    def compute_stats():
        return json.dumps(compute_statistics(provider_slug))

    if force:
        logger.info("Force Statistics Recompute.")
        stats = compute_stats()
        cache.set(cache_key, stats, timeout=DEFAULT_CACHE_EXPIRATION)
    else:
        # get_or_set needs a callable to avoid evaluating early.
        # compute stats will only be evaluated if the cache is not set.
        stats = cache.get_or_set(cache_key, compute_stats, timeout=DEFAULT_CACHE_EXPIRATION)

    return json.loads(stats)


def get_default_stat():
    return {"duration": [], "area": [], "size": [], "mpp": []}


def get_accessors():
    return {
        # Get the size in MBs per unit area (valid for tasks objects)
        "size": lambda t, area_km: t.result.size / area_km,
        # Get the duration per unit area (valid for export_run, data_provider_task_records, or export_task_records)
        "duration": lambda o, area_km: parse_duration(o.duration) / area_km if o.duration else 0,
        # Get the area from the run or use the parent's area
        "area": lambda o, area_km: area_km,
    }


def update_all_statistics_caches(executor=ProcessPoolExecutor):
    """
    A helper function to compute all of the statistics and update all caches at once.
    :param executor: The concurrent.futures.executor to use for processing the statistics.
    """
    provider_slugs = [provider.slug for provider in DataProvider.objects.all()]
    for conn in db.connections.all():
        conn.close_if_unusable_or_obsolete()
    with executor() as pool:
        pool.map(get_statistics, provider_slugs, itertools.repeat(True))


def get_default_tile_grid(level=10):
    """
    A geodetic grid for EPSG:4326 containing only the specified tile level
    :param level: The desired level for the tiling grid
    :return:
    """
    tmp = mapproxy_grid.tile_grid_for_epsg("EPSG:4326")
    res = tmp.resolution(level)

    return mapproxy_grid.tile_grid_for_epsg("EPSG:4326", res=[res])


def has_tiles(export_task_record_name):
    """
    Some output types are spatially invariant and it is a waste to try and build per-tile statistics for them. Also
    the Project File will contain a copy of each export format which could grossly over estimate data in a region
    (i.e. a job may export gpkg and shapefile so the project.zip will contain 2x copies of the data)
    :param export_task_record_name: The name of the ExportTaskRecord
    :return: True if the result of this task should be included in per-tile statistics
    """
    return export_task_record_name not in [
        "Area of Interest (.geojson)",
        "Project File (.zip)",
    ]


def compute_statistics(provider_slug, tile_grid=get_default_tile_grid(), filename=None):
    """
    :param export_task_records: ExporTaskRecords is a list of all export tasks
    :param get_group: Function to generate a group id given a DataExportProviderTask
    :param tile_grid: Calculate statistics for each tile in the tile grid
    :param filename: Serializes the intermediate data-sample data so it can be shared btw different deployments
    :return: A dict with statistics including area, duration, and package size per sq. kilometer
    """

    max_estimate_export_task_records = os.getenv("MAX_ESTIMATE_EXPORT_TASK_RECORDS", 10000)
    # Order by time descending to ensure more recent samples are collected first
    export_task_records: QuerySet[ExportTaskRecord] = (
        ExportTaskRecord.objects.filter(
            export_provider_task__provider__slug=provider_slug,
            status=TaskState.SUCCESS.value,
            export_provider_task__status=TaskState.COMPLETED.value,
            result__isnull=False,
            # Only use results larger than a MB,
            # anything less is likely a failure or a test.
            result__size__gt=1,
        )
        .order_by("-finished_at")
        .select_related("result", "export_provider_task__run__job", "export_provider_task__provider")
        .all()[:max_estimate_export_task_records]
    )

    processed_runs = {}
    processed_dptr = {}
    export_task_count = 0
    total_count = len(export_task_records)  # This should be the first and only DB hit.

    all_stats = {}

    logger.debug("Prefetching geometry data from all Jobs")

    logger.info(f"Beginning collection of statistics for {total_count} {provider_slug} ExportTaskRecords")
    runs: List[ExportRun] = list(
        set([export_task_record.export_provider_task.run for export_task_record in export_task_records])
    )
    data_provider_task_records: List[DataProviderTaskRecord] = list(
        set([export_task_record.export_provider_task for export_task_record in export_task_records])
    )
    default_stat = get_default_stat()
    accessors = get_accessors()
    global_stats = get_child_entry(all_stats, global_key, default_stat)

    for run in runs:
        area = get_geometry_description(run.job.the_geom)["area"]
        collect_samples(run, [global_stats], ["duration", "area"], accessors, area)

    for data_provider_task_record in data_provider_task_records:
        area = get_geometry_description(data_provider_task_record.run.job.the_geom)["area"]
        provider_stats = get_child_entry(all_stats, data_provider_task_record.provider.slug, default_stat)
        collect_samples(data_provider_task_record, [provider_stats], ["duration", "area"], accessors, area)

    collected_stats = collect_samples_for_export_task_records(export_task_records, copy.deepcopy(all_stats), tile_grid)
    [all_stats.update(stat) for stat in collected_stats]

    logger.info(
        f"Computing statistics across {export_task_count} completed "
        f"{provider_slug} ExportTaskRecords (geom_cache_misses={_dbg_geom_cache_misses})"
    )

    # TODO: Merge in any auxiliary sample data?

    if filename is not None:
        all_stats["timestamp"] = str(datetime.datetime.now())
        with open(filename, "w") as file:
            json.dump(all_stats, file)

    totals = {
        "run_count": len(processed_runs),
        "data_provider_task_count": len(processed_dptr),
        "export_task_count": export_task_count,
    }
    returned_totals = process_totals_concurrently(list(all_stats.keys()), copy.deepcopy(all_stats))
    [totals.update(total) for total in returned_totals]
    tile_count = sum([provider.get("tile_count", 0) for slug, provider in totals.items() if isinstance(provider, dict)])
    logger.info("Generated statistics for %d tiles for group %s", tile_count, provider_slug)
    return totals


def process_totals(provider_slug: str, stats: dict) -> dict:

    if provider_slug in ["timestamp"]:
        return dict()

    totals = {provider_slug: get_summary_stats(stats[provider_slug], ("area", "duration", "size", "mpp"))}
    tile_count = 0

    for task_name in stats[provider_slug]:
        if task_name in ["duration", "area", "size", "mpp"]:
            # These are properties on the roll'ed up statistics
            continue
        elif task_name.startswith("tile_"):
            # Two-level map, index by y then x+z
            y_s = stats[provider_slug][task_name]
            total_ys = {}
            totals[provider_slug][task_name] = total_ys
            for xz_s in y_s:
                total_ys[xz_s] = get_summary_stats(y_s[xz_s], ("area", "duration", "size", "mpp"))
                total_ys[xz_s]["tile_coord"] = y_s[xz_s]["tile_coord"]
                tile_count += 1
        else:
            totals[provider_slug][task_name] = get_summary_stats(
                stats[provider_slug][task_name], ("area", "duration", "size")
            )

    totals[provider_slug]["tile_count"] = tile_count
    return totals


def process_totals_concurrently(provider_slugs, stats):
    thread = threading.current_thread()
    executor = ThreadPoolExecutor
    if thread.daemon:
        executor = ProcessPoolExecutor
    with executor() as pool:
        totals = pool.map(
            process_totals, provider_slugs, itertools.repeat(stats), chunksize=math.ceil(len(provider_slugs) / 8)
        )
        return totals


def collect_samples_for_export_task_record(export_task_record: ExportTaskRecord, stats: dict, tile_grid):

    default_stat = get_default_stat()
    accessors = get_accessors()
    global_stats = stats["GLOBAL"]
    geometry_description = get_geometry_description(export_task_record.export_provider_task.run.job.the_geom)
    area = geometry_description["area"]
    provider_stats = get_child_entry(stats, export_task_record.export_provider_task.provider.slug, default_stat)

    task_stats = get_child_entry(provider_stats, export_task_record.name, default_stat)

    if has_tiles(export_task_record.name):
        cache_key = f"{tid_cache_prefix}.{export_task_record.export_provider_task.run}"
        affected_tile_stats = get_tile_stats(
            provider_stats, tile_grid, geometry_description["bbox"], True, cache_key=cache_key
        )
    else:
        affected_tile_stats = []

    collect_samples(
        export_task_record,
        affected_tile_stats + [task_stats],
        ["duration", "area", "size"],
        accessors,
        area,
    )

    sz = accessors["size"](export_task_record, area)
    provider_stats["size"] += [sz]  # Roll-up into provider_task level
    global_stats["size"] += [sz]  # Roll-up into global level

    # Collect a sample of the megabytes per pixel
    if has_tiles(export_task_record.name):
        try:
            provider = export_task_record.export_provider_task.provider
            mpp = compute_mpp(provider, geometry_description["bbox"], export_task_record.result.size)
            if len(provider_stats["mpp"]) < MAX_SAMPLES_PER_TARGET:
                provider_stats["mpp"] += [mpp]
            if len(global_stats["mpp"]) < MAX_SAMPLES_PER_TARGET:
                global_stats["mpp"] += [mpp]
            for ts in affected_tile_stats:
                if len(ts["mpp"]) < MAX_SAMPLES_PER_TARGET:
                    ts["mpp"] += [mpp]

        except ObjectDoesNotExist:
            pass
    return stats


def collect_samples_for_export_task_records(export_task_records, stats, tile_grid):
    # Can't do this concurrently because proj can't be pickled (i.e. tile_grid)
    return map(
        collect_samples_for_export_task_record,
        export_task_records,
        itertools.repeat(stats),
        itertools.repeat(tile_grid),
    )


def get_child_entry(parent, key, default=None):
    """
    Helper method for accessing an element in a dictionary that optionally inserts missing elements
    :param parent: The dictionary to search
    :param key: The lookup key
    :param default: A default value if key is missing on parent, if default is None missing elements are not filled
    :return:
    """
    if key not in parent:
        if default is not None:
            parent[key] = default.copy()
        else:
            return None

    return parent[key]


def get_tile_stats(parent, tile_grid, bbox, create_if_absent=False, cache_key=None):
    """
    Intersects the bbox with the tile grid, returning all of the corresponding objects that hold
    data samples for those tiles

    :param parent: Parent dictionary
    :param tile_grid: The tile grid defining per-tile statistics to generate
    :param bbox: Query bounding box
    :param create_if_absent: True if objects should be created if not-currently there
    :param tid_cache: Optional cache storing results of bbox query on grid
    :param cid: Optional id used to access/store results of bbox query on grid, tid_cache MUST be provided when used
    :return: The list of objects intersecting the bbox, or an empty array
    """

    run_bbox = mapproxy_grid.grid_bbox(bbox, bbox_srs=mapproxy_srs.SRS(4326), srs=tile_grid.srs)
    affected_tiles = tile_grid.get_affected_level_tiles(run_bbox, tile_grid.levels - 1)  # Use highest res grid

    tile_coords = []
    for tile_coord in affected_tiles[2]:
        tile_coords += [tile_coord]

    tile_stats = list(map(lambda t: get_tile_stat(parent, t, create_if_absent), tile_coords))

    if create_if_absent:
        return tile_stats  # We won't have None entries
    else:
        return [x for x in tile_stats if x is not None]


def get_tile_stat(parent, tile_coord, create_if_absent=False):
    """
    :param parent: Parent dictionary
    :param tile_coord: (x, y, z) of the desired tile
    :param create_if_absent: True if objects should be created if not-currently there
    :return: Corresponding object holding data stats of the requested tile
    """
    x, y, z = tile_coord

    y_id = "tile_{}".format(y)
    y_s = get_child_entry(parent, y_id, {} if create_if_absent else None)
    xz_s = None

    if y_id in parent:
        # Currently we only cache 1 z-level so we will only use 2-level map not 3...
        default_val = {"duration": [], "size": [], "mpp": [], "tile_coord": tile_coord} if create_if_absent else None
        xz_s = get_child_entry(y_s, "{}_{}".format(x, z), default_val)

    return xz_s


def collect_samples(item, targets, fields, accessors, area):
    """
    Appends data points to the list of samples on each target object
    :param item: Item containing a single data point for each field in fields
    :param targets: List of objects collecting the data points
    :param fields: List of fields to collect
    :param accessors: Dictionary of access method applied to item to generate a data point
    :param area: The area in sq. km used for normalizing data points (e.g. size per sq km)
    """
    for i in range(0, len(fields)):
        # Get the value for this field
        field = fields[i]
        sample = accessors[field](item, area)

        if sample:
            # Append to to each target that is interested in that value
            for target in targets:
                if field in target and len(target[field]) < MAX_SAMPLES_PER_TARGET:
                    target[field] += [sample]
    return targets


def compute_mpp(provider, bbox, size_mb, srs="4326", with_clipping=True):
    """
    Computes the megabytes per pixel within the specified bounding box
    :param provider: The DataProvider
    :param bbox: The bounding box defining the export region
    :param size_mb: The size of the export output in megabytes
    :param srs: The SRS of the bounding box
    :param with_clipping: When True tiles within the bbox will also have any pixels outside
                          the bbox excluded.  False will use all pixels from tiles that intersect the bbox
    :return: Megabytes per pixel
    """
    tile_grid = get_provider_grid(provider)
    return size_mb / get_total_num_pixels(tile_grid, bbox, srs, with_clipping)


def get_total_num_pixels(tile_grid, bbox, srs="4326", with_clipping=True):
    """
    Determine the number of pixels in the tile_grid (across all levels) that are within the specified bounding box
    :param tile_grid:
    :param bbox: The bounding box defining the export region
    :param srs: The SRS of the bounding box
    :param with_clipping: When True tiles within the bbox will also have any pixels outside
                          the bbox excluded.  False will use all pixels from tiles that intersect the bbox
    :return: Total number of pixels
    """
    grid_bbox = mapproxy_grid.grid_bbox(bbox, mapproxy_srs.SRS(srs), tile_grid.srs)

    # Determine all affected tiles across all provider levels
    total_pixels = 0
    px_per_tile = tile_grid.tile_size[0] * tile_grid.tile_size[1]

    for lvl in range(0, tile_grid.levels):
        result = tile_grid.get_affected_level_tiles(grid_bbox, int(lvl))
        tile_coords = result[2]

        if with_clipping:
            # Determine number of pixels contained in the bbox ONLY
            for tile_coord in tile_coords:
                tile_bbox = tile_grid.tile_bbox(tile_coord, True)
                i = get_bbox_intersect(grid_bbox, tile_bbox)

                # Assume uniform spacing of pixels in the tile
                tile_num_px = (get_area_bbox(i) / get_area_bbox(tile_bbox)) * px_per_tile
                total_pixels += tile_num_px
        else:
            num_tiles = result[1][0] * result[1][1]  # xdim * ydim
            total_pixels += px_per_tile * num_tiles

    return total_pixels


def get_summary_stats(input_item, fields):
    """
    Computes all statistics at a particular granularity defined by the input_item across each
    of the fields of interest (e.g. 'areas', 'times', 'sizes').  input_item could represent
    all samples from 'osm' providers, or all 'runs', or all tasks at a particular tile in a tile grid
    :param input_item: The item containing arrays holding all data samples
    :param fields: The list of fields of interest, input_item should contain a list of samples for each field
    :return: Object holding the computed statistics for each field
    """
    target = {}

    def compute_stats_for(field):
        # Computes basic statistics for a single field (e.g. 'sizes')
        value_list = input_item.get(field)
        if value_list and len(value_list) > 0:
            st = dict()
            st["mean"] = statistics.mean(value_list)

            # These items aren't currently used for estimate do not waste time calculating them.
            # st["min"] = min(value_list)
            # st["max"] = max(value_list)
            # st["count"] = len(value_list)
            #
            # if len(value_list) >= 2:
            #     st["variance"] = statistics.variance(value_list, st["mean"])
            #     st["ci_90"] = get_confidence_interval(st["mean"], math.sqrt(st["variance"]), st["count"], 1.645)
            #     st["ci_95"] = get_confidence_interval(st["mean"], math.sqrt(st["variance"]), st["count"], 1.960)
            #     st["ci_99"] = get_confidence_interval(st["mean"], math.sqrt(st["variance"]), st["count"], 2.580)

            target[field] = st

    for fld in fields:
        compute_stats_for(fld)

    return target


def get_confidence_interval(mean, std_dev, sample_size, ci=1.96):
    """
    Computes the confidence interval assuming a normal distribution of the data
    :param mean: The sample mean
    :param std_dev: The standard deviation (ideally population - otherwise sample std.dev)
    :param sample_size: The number of samples used to estimate mean and std_dev
    :param ci: The z-* multiplier for the desired confidence interval (e.g. 1.96 = 95% CI)
    :return: [lower_bound, upper_bound] of the specified confidence interval
    """
    error_margin = ci * (std_dev / math.sqrt(sample_size))
    return [mean - error_margin, mean + error_margin]


def get_provider_grid(provider, min_zoom=None, max_zoom=None):
    """
    :param provider: The DataProvider
    :return: The tile grid used by the DataProvider
    """
    # Set custom zoom levels if available, otherwise use the provider defaults.
    min_zoom = int(min_zoom) if min_zoom else provider.level_from
    max_zoom = int(max_zoom) if max_zoom else provider.level_to

    # TODO: Pull this provider's grid out of it's config
    levels = list(range(min_zoom, max_zoom + 1))
    tmp = mapproxy_grid.tile_grid_for_epsg("EPSG:4326")
    res = list(map(lambda l: tmp.resolution(l), levels))

    return mapproxy_grid.tile_grid_for_epsg("EPSG:4326", tile_size=(256, 256), res=res)


def query(
    provider_slug,
    field,
    statistic_name,
    bbox,
    bbox_srs,
    gap_fill_thresh=0.1,
    default_value=None,
    custom_stats=None,
):
    """
    Finds the highest resolution of the requested statistic:
        1. Within group and bbox region (leveraging tile grid)
        2. Within group
        3. Global
        4. Default value

    :param provider_slug: Must match one of your current data provider's slug values.
    :param field: The field to query (e.g. size, mpp, duration)
    :param statistic_name: The name of the statistic (e.g. mean, ci_90, ..)
    :param bbox: The bounding box defining the region to estimate
    :param bbox_srs: The srs of bbox
    :param gap_fill_thresh: If bbox has less that this % overlap with tile-level statistics then fill-gaps using group
                            level or global level size/sq.km, otherwise gap-fill using the mean of observed tile-level
                            stats
    :param default_value: A default value to return if no statistics exist
    :param custom_stats: Use custom statistics dictionary rather than cached (see get_statistics or compute_statistics)
    :return: (Highest resolution of statistic, object w/ metadata about how it was generated incl. resolution)
    """
    if custom_stats is not None:
        all_stats = custom_stats
    else:
        all_stats = get_statistics(provider_slug)

    method = {
        "stat": statistic_name,
    }

    def get_single_value(o):
        fld = o.get(field)
        if fld:
            return fld.get(statistic_name)

    def get_upper_ci_value(o):
        fld = o.get(field)
        if fld and fld.get(statistic_name):  # Can be missing (e.g. 1 data sample)
            return fld[statistic_name][1]  # Get the upper bound

    get_value = get_upper_ci_value if statistic_name.startswith("ci_") else get_single_value
    stat_value = None

    if all_stats:
        provider_stats = all_stats.get(provider_slug)
        if provider_stats:
            # TODO tile_grid params should be serialized on all_stats object
            # We have some statistics specific to this group (e.g. osm, wms, etc)
            tile_grid = get_default_tile_grid()
            req_bbox = mapproxy_grid.grid_bbox(bbox, mapproxy_srs.SRS(bbox_srs), tile_grid.srs)
            req_area = get_area_bbox(req_bbox)

            affected_tiles = get_tile_stats(provider_stats, tile_grid, req_bbox)
            if affected_tiles and len(affected_tiles) > 0:
                # We have some stats specific to this group, at tiles within the user-defined region
                # We want to weight tile-specific statistics based on its % overlap with the bbox
                # (e.g. tile1 accounts for 50% of bbox area, its stats should be weighted at 50%
                total_weight = 0
                values = []
                stat_value = 0

                for tile_stat in affected_tiles:
                    t_val = get_value(tile_stat)
                    if t_val is not None:
                        tile_coord = tile_stat["tile_coord"]
                        inter = get_bbox_intersect(req_bbox, tile_grid.tile_bbox(tile_coord, True))
                        weight = get_area_bbox(inter) / req_area

                        stat_value += weight * t_val
                        values += [t_val]
                        total_weight += weight

                if total_weight > 1.0:
                    # Shouldn't happen since tile-grid is disjoint...
                    stat_value /= total_weight
                elif total_weight < gap_fill_thresh:
                    # If the overlap was very minor than gap fill using the group-wide stats
                    stat_value += (1.0 - total_weight) * get_value(provider_stats)
                else:
                    # Otherwise, gap fill using the average from the tiles we did see
                    stat_value += (1.0 - total_weight) * statistics.mean(values)

                if total_weight > 0:
                    method["group"] = "{}_tiles".format(provider_slug)
                    method["tiles"] = {
                        "count": len(affected_tiles),
                        "total_weight": 100 * total_weight,
                        "gap_fill": provider_slug if total_weight < gap_fill_thresh else "tile_mean",
                    }
                else:
                    # It's possible that none of the tiles had the stat we were looking for in which case gap_fill
                    # is essentially the same as using the group-level statistic
                    method["group"] = provider_slug
            else:
                # No overlapping tiles, use group specific stats
                method["group"] = provider_slug
                stat_value = get_value(provider_stats)
        elif global_key in all_stats:
            # No group-specific data, use statistics computed across all groups (i.e. every completed job)
            method["group"] = global_key
            stat_value = get_value(all_stats[global_key])

    if stat_value is None:
        # No statistics... use default
        method["group"] = "None"
        stat_value = default_value

    return stat_value, method
