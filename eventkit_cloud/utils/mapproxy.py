import copy
import logging
import multiprocessing
import os
import sqlite3
import time
from multiprocessing import Process
from multiprocessing.dummy import DummyProcess
from typing import Tuple

import mapproxy
import yaml
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.gis.geos import GEOSGeometry
from django.core.cache import cache
from django.db import connections
from mapproxy.config.config import load_config, load_default_config
from mapproxy.config.loader import (
    ProxyConfiguration,
    ConfigurationError,
    validate_references,
)
from mapproxy.wsgiapp import MapProxyApp
from webtest import TestApp

from eventkit_cloud.core.helpers import get_cached_model
from eventkit_cloud.jobs.helpers import get_valid_regional_justification
from eventkit_cloud.tasks import get_cache_value
from eventkit_cloud.tasks.enumerations import TaskState
from eventkit_cloud.utils import auth_requests
from eventkit_cloud.utils.geopackage import (
    get_tile_table_names,
    set_gpkg_contents_bounds,
    get_table_tile_matrix_information,
    get_zoom_levels_table,
    remove_empty_zoom_levels,
)
from eventkit_cloud.utils.stats.eta_estimator import ETA

# Mapproxy uses processes by default, but we can run child processes in demonized process, so we use
# a dummy process which relies on threads.  This fixes a bunch of deadlock issues which happen when using billiard.
multiprocessing.Process = DummyProcess
from mapproxy.seed import seeder  # noqa: E402
from mapproxy.seed.config import SeedingConfiguration  # noqa: E402
from mapproxy.seed.util import ProgressLog, exp_backoff, timestamp, ProgressStore  # noqa: E402

multiprocessing.Process = Process


logger = logging.getLogger(__name__)

# mapproxy.client.log names its logger with the string 'mapproxy.source.request', we have to use that string
# to capture the logger. Should be taken to see if mapproxy ever changes the name of that logger.
log_settings = settings.MAPPROXY_LOGS
client_logger = logging.getLogger("mapproxy.source.request")
client_logger.setLevel(settings.LOG_LEVEL if log_settings.get("requests", False) else logging.ERROR)

mapproxy_config_keys_index = "mapproxy-config-cache-keys"


def get_mapproxy_config_template(slug, user=None):
    if user:
        return f"mapproxy-config-{user}-{slug}"
    else:
        return f"mapproxy-config-{slug}"


class CustomLogger(ProgressLog):
    def __init__(self, task_uid=None, *args, **kwargs):
        self.task_uid = task_uid
        super(CustomLogger, self).__init__(*args, **kwargs)
        # Log mapproxy status but allow a setting to reduce database writes.
        self.log_step_step = 1
        self.log_step_counter = self.log_step_step
        self.eta = ETA(task_uid=task_uid)
        self.interval = 1

    def log_step(self, progress):
        from eventkit_cloud.tasks.task_process import update_progress

        self.eta.update(progress.progress)  # This may also get called by update_progress but because update_progress
        # is rate-limited; we also do it here to get more data points for making
        # better eta estimates

        if self.task_uid:

            if self.log_step_counter == 0:
                if (
                    get_cache_value(uid=self.task_uid, attribute="status", model_name="ExportTaskRecord")
                    == TaskState.CANCELED.value
                ):
                    logger.error(f"The task uid: {self.task_uid} was canceled. Exiting...")
                    raise Exception("The task was canceled.")

                update_progress(self.task_uid, progress=progress.progress * 100, eta=self.eta)
                self.log_step_counter = self.log_step_step
            self.log_step_counter -= 1

        # Old version of super.log_step that includes ETA string
        # https://github.com/mapproxy/mapproxy/commit/93bc53a01318cd63facdb4ee13968caa847a5c17
        if not self.verbose:
            return
        if (self._laststep + self.interval) < time.time():
            logger.info(
                f"[{timestamp()}] {progress.progress * 100:6.2f}%\t{progress.progress_str.ljust(20)} ETA: {self.eta}\r"
            )
            # [12:24:08] 100.00%     000000               ETA: 2020-08-06-12:22:30-UTC
            self._laststep = time.time()


def get_custom_exp_backoff(max_repeat=None, task_uid=None):
    def custom_exp_backoff(*args, **kwargs):
        if max_repeat:
            kwargs["max_repeat"] = max_repeat
        if get_cache_value(uid=task_uid, attribute="status", model_name="ExportTaskRecord") == TaskState.CANCELED.value:
            logger.error(f"The task uid: {task_uid} was canceled. Exiting...")
            raise Exception("The task was canceled.")
        exp_backoff(*args, **kwargs)

    return custom_exp_backoff


class MapproxyGeopackage(object):
    """
    Convert a External service to a geopackage.
    """

    def __init__(
        self,
        config=None,
        gpkgfile=None,
        bbox=None,
        service_url=None,
        layer="default",
        debug=None,
        name=None,
        level_from=None,
        level_to=None,
        service_type=None,
        task_uid=None,
        selection=None,
        projection=None,
        input_gpkg=None,
    ):
        """
        Initialize the ExternalServiceToGeopackage utility.

        Args:
            gpkgfile: where to write the gpkg output
            debug: turn debugging on / off
        """
        self.gpkgfile = gpkgfile
        self.bbox = bbox
        self.service_url = service_url
        self.debug = debug
        self.name = name
        self.level_from = level_from
        self.level_to = level_to
        self.layer = layer
        self.config = config
        self.service_type = service_type
        self.task_uid = task_uid
        self.selection = selection
        self.projection = projection
        self.input_gpkg = input_gpkg

    def build_config(self):
        pass

    def get_check_config(self):
        """
        Create a MapProxy configuration object and verifies its validity
        """
        if self.config or self.projection:
            conf_dict = yaml.safe_load(self.config) or dict()
        else:
            raise ConfigurationError("MapProxy configuration is required for raster data providers")

        if not conf_dict.get("grids"):
            conf_dict["grids"] = {
                "default": {"srs": "EPSG:4326", "tile_size": [256, 256], "origin": "nw"},
                "webmercator": {"srs": "EPSG:3857", "tile_size": [256, 256], "origin": "nw"},
            }
        elif self.projection:
            conf_dict["grids"].update(
                {str(self.projection): {"srs": f"EPSG:{self.projection}", "tile_size": [256, 256], "origin": "nw"}}
            )

        # If user provides a cache setup then use that and substitute in the geopackage file for the placeholder.
        conf_dict["caches"] = conf_dict.get("caches", {})
        try:
            conf_dict["caches"]["default"]["cache"]["filename"] = self.gpkgfile
        except KeyError:
            conf_dict["caches"]["default"] = get_cache_template(
                ["{0}".format(self.layer)],
                [grids for grids in conf_dict.get("grids")],
                self.gpkgfile,
                table_name=self.layer,
            )

        if self.projection:
            conf_dict["caches"]["repro_cache"] = copy.deepcopy(conf_dict["caches"]["default"])
            conf_dict["caches"]["repro_cache"]["cache"]["filename"] = self.input_gpkg
            conf_dict["caches"]["repro_cache"]["sources"] = []
            conf_dict["caches"]["default"]["meta_size"] = [4, 4]
            conf_dict["caches"]["default"]["bulk_meta_tiles"] = True
            conf_dict["caches"]["default"] = get_cache_template(
                ["repro_cache"], [str(self.projection)], self.gpkgfile, "default"
            )

        # Need something listed as a service to pass the mapproxy validation.
        conf_dict["services"] = ["demo"]

        # disable SSL cert checks

        ssl_verify = getattr(settings, "SSL_VERIFICATION", True)
        if isinstance(ssl_verify, bool):
            if not ssl_verify:
                conf_dict["globals"] = {"http": {"ssl_no_cert_checks": ssl_verify}}
        else:
            conf_dict["globals"] = {"http": {"ssl_ca_certs": ssl_verify}}

        # Add autoconfiguration to base_config
        mapproxy_config = load_default_config()
        load_config(mapproxy_config, config_dict=conf_dict)

        # Create a configuration object
        mapproxy_configuration = ProxyConfiguration(mapproxy_config, seed=seeder.seed, renderd=None)

        # # As of Mapproxy 1.9.x, datasource files covering a small area cause a bbox error.
        if self.bbox:
            if isclose(self.bbox[0], self.bbox[2], rel_tol=0.001) or isclose(self.bbox[1], self.bbox[3], rel_tol=0.001):
                logger.warning("Using bbox instead of selection, because the area is too small")
                self.selection = None

        seed_dict = get_seed_template(
            bbox=self.bbox,
            level_from=self.level_from,
            level_to=self.level_to,
            coverage_file=self.selection,
            projection=self.projection,
        )

        # Create a seed configuration object
        seed_configuration = SeedingConfiguration(seed_dict, mapproxy_conf=mapproxy_configuration)
        errors = validate_references(conf_dict)
        if errors:
            logger.error("MapProxy configuration failed.")
            logger.error("Using Configuration:")
            logger.error(conf_dict)
            raise ConfigurationError("MapProxy returned the error - {0}".format(", ".join(errors)))

        return conf_dict, seed_configuration, mapproxy_configuration

    # @retry
    def convert(
        self,
    ):
        """
        Convert external service to gpkg.
        """

        from eventkit_cloud.tasks.task_process import TaskProcess

        conf_dict, seed_configuration, mapproxy_configuration = self.get_check_config()

        #  Customizations...
        mapproxy.seed.seeder.exp_backoff = get_custom_exp_backoff(
            max_repeat=int(conf_dict.get("max_repeat", 5)), task_uid=self.task_uid
        )

        logger.info("Beginning seeding to {0}".format(self.gpkgfile))
        try:
            conf = yaml.safe_load(self.config) or dict()
            cert_info = conf.get("cert_info")
            auth_requests.patch_https(cert_info=cert_info)

            cred_var = conf.get("cred_var")
            auth_requests.patch_mapproxy_opener_cache(slug=self.name, cred_var=cred_var)

            progress_store = get_progress_store(self.gpkgfile)
            progress_logger = CustomLogger(
                task_uid=self.task_uid,
                progress_store=progress_store,
                verbose=log_settings.get("verbose"),
                silent=log_settings.get("silent"),
            )
            task_process = TaskProcess(task_uid=self.task_uid)
            task_process.start_process(
                lambda: seeder.seed(
                    tasks=seed_configuration.seeds(["seed"]),
                    concurrency=get_concurrency(conf_dict),
                    progress_logger=progress_logger,
                )
            )
            check_zoom_levels(self.gpkgfile, mapproxy_configuration)
            remove_empty_zoom_levels(self.gpkgfile)
            set_gpkg_contents_bounds(self.gpkgfile, self.layer, self.bbox)
        except Exception as e:
            logger.error("Export failed for url {}.".format(self.service_url))
            logger.error(e)
            raise
        finally:
            connections.close_all()
        return self.gpkgfile


def get_progress_store(gpkg):
    progress_file = os.path.join(os.path.dirname(gpkg), ".progress_logger")
    return ProgressStore(filename=progress_file, continue_seed=True)


def get_cache_template(sources, grids, geopackage, table_name="tiles"):
    """
    Returns the cache template which is "controlled" settings for the application.

    The intent is to allow the user to configure certain things but impose specific behavior.
    :param sources: A name for the source
    :param grids: specific grid for the data source
    :param geopackage: Location for the geopackage
    :return: The dict template
    """
    if sources == ["None"]:
        sources = []
    return {
        "sources": sources,
        "cache": {"type": "geopackage", "filename": str(geopackage), "table_name": table_name},
        "grids": [grid for grid in grids if grid == "default"] or grids,
        "format": "mixed",
        "request_format": "image/png",
    }


def get_seed_template(bbox=None, level_from=None, level_to=None, coverage_file=None, projection=None):
    out_projection = 4326
    if projection:
        out_projection = projection
    seed_template = {
        "coverages": {"geom": {"srs": "EPSG:4326"}},
        "seeds": {
            "seed": {
                "coverages": ["geom"],
                "refresh_before": {"minutes": 0},
                "levels": {"to": level_to or 10, "from": level_from or 0},
                "caches": ["default"],
            }
        },
    }

    if projection:
        seed_template["seeds"]["seed"]["grids"] = [str(out_projection)]
    if coverage_file:
        seed_template["coverages"]["geom"]["datasource"] = str(coverage_file)
    else:
        seed_template["coverages"]["geom"]["srs"] = f"EPSG:{out_projection}"
        seed_template["coverages"]["geom"]["bbox"] = bbox

    return seed_template


def isclose(a, b, rel_tol=1e-09, abs_tol=0.0):
    return abs(a - b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)


def check_zoom_levels(gpkg, mapproxy_configuration):
    try:
        grid = mapproxy_configuration.caches.get("default").conf.get("grids")[0]
        tile_size = mapproxy_configuration.grids.get(grid).conf.get("tile_size")
        tile_grid = mapproxy_configuration.grids.get(grid).tile_grid()
        for table_name in get_tile_table_names(gpkg):
            actual_zoom_levels = get_zoom_levels_table(gpkg, table_name)
            gpkg_tile_matrix = get_table_tile_matrix_information(gpkg, table_name)
            for actual_zoom_level in actual_zoom_levels:
                if actual_zoom_level not in [level.get("zoom_level") for level in gpkg_tile_matrix]:
                    res = tile_grid.resolution(actual_zoom_level)
                    grid_sizes = tile_grid.grid_sizes[actual_zoom_level]
                    with sqlite3.connect(gpkg) as conn:
                        conn.execute(
                            """
INSERT OR REPLACE INTO gpkg_tile_matrix (table_name, zoom_level, matrix_width, matrix_height, tile_width, tile_height,
pixel_x_size, pixel_y_size)
VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                table_name,
                                actual_zoom_level,
                                grid_sizes[0],
                                grid_sizes[1],
                                tile_size[0],
                                tile_size[1],
                                res,
                                res,
                            ),
                        )
    except Exception as e:
        logger.error("Problem in check_zoom_levels: {}".format(e))
        logger.error("Check provider MapProxy configuration.")


def get_concurrency(conf_dict):
    concurrency = conf_dict.get("concurrency")
    if not concurrency:
        concurrency = getattr(settings, "MAPPROXY_CONCURRENCY", 1)
    return int(concurrency)


def create_mapproxy_app(slug: str, user: User = None) -> TestApp:
    mapproxy_config_key = get_mapproxy_config_template(slug, user=user)
    mapproxy_config = cache.get(mapproxy_config_key)
    conf_dict = cache.get_or_set(f"base-config-{slug}", lambda: get_conf_dict(slug), 360)
    if not mapproxy_config:
        # TODO: place this somewhere else consolidate settings.
        base_config = {
            "services": {
                "demo": None,
                "tms": None,
                "wmts": {
                    "featureinfo_formats": [
                        {"mimetype": "application/json", "suffix": "json"},
                        {"mimetype": "application/gml+xml; version=3.1", "suffix": "gml"},
                    ]
                },
            },
            # Cache based on slug so that the caches don't overwrite each other.
            "caches": {slug: {"cache": {"type": "file"}, "sources": ["default"], "grids": ["default"]}},
            "layers": [{"name": slug, "title": slug, "sources": [slug]}],
            "globals": {"cache": {"base_dir": getattr(settings, "TILE_CACHE_DIR")}},
        }
        if conf_dict["sources"].get("info"):
            base_config["caches"][slug]["sources"] += ["info"]
        if conf_dict["sources"].get("footprint"):
            base_config["caches"][get_footprint_layer_name(slug)] = {
                "cache": {"type": "file"},
                "sources": ["footprint"],
                "grids": ["default"],
            }
            base_config["layers"] += [
                {
                    "name": get_footprint_layer_name(slug),
                    "title": get_footprint_layer_name(slug),
                    "sources": [get_footprint_layer_name(slug)],
                }
            ]
        base_config, conf_dict = add_restricted_regions_to_config(base_config, conf_dict, slug, None)
        try:
            mapproxy_config = load_default_config()
            load_config(mapproxy_config, config_dict=base_config)
            load_config(mapproxy_config, config_dict=conf_dict)
            mapproxy_configuration = ProxyConfiguration(mapproxy_config)

            if settings.REGIONAL_JUSTIFICATION_TIMEOUT_DAYS:
                regional_justification_timeout = settings.REGIONAL_JUSTIFICATION_TIMEOUT_DAYS * 86400
            else:
                regional_justification_timeout = None
            mapproxy_configs_set = cache.get_or_set(mapproxy_config_keys_index, set())
            mapproxy_configs_set.add(mapproxy_config_key)
            cache.set(mapproxy_config_keys_index, mapproxy_configs_set)
            cache.set(mapproxy_config_key, mapproxy_config, regional_justification_timeout)

        except ConfigurationError as e:
            logger.error(e)
            raise
    else:
        try:
            mapproxy_configuration = ProxyConfiguration(mapproxy_config)
        except ConfigurationError as e:
            logger.error(e)
            raise

    cert_info = conf_dict.get("cert_info")
    auth_requests.patch_https(cert_info=cert_info)

    cred_var = conf_dict.get("cred_var")
    auth_requests.patch_mapproxy_opener_cache(slug=slug, cred_var=cred_var)

    app = MapProxyApp(mapproxy_configuration.configured_services(), mapproxy_config)

    return TestApp(app)


def get_conf_dict(slug: str) -> dict:
    """
    Takes a slug value for a DataProvider and returns a mapproxy configuration as a dict.
    :param slug: A string matching the slug of a DataProvider
    :return: a dict.
    """
    from eventkit_cloud.jobs.models import DataProvider  # Circular reference

    try:
        provider = get_cached_model(model=DataProvider, prop="slug", value=slug)
    except Exception:
        raise Exception(f"Unable to find provider for slug {slug}")

        # Load and "clean" mapproxy config for displaying a map.
    try:
        conf_dict = yaml.safe_load(provider.config)

        # Pop layers out so that the default layer configuration above is used.
        conf_dict.pop("layers", "")
        ssl_verify = getattr(settings, "SSL_VERIFICATION", True)
        if isinstance(ssl_verify, bool):
            if not ssl_verify:
                conf_dict["globals"] = {"http": {"ssl_no_cert_checks": ssl_verify}}
        else:
            conf_dict["globals"] = {"http": {"ssl_ca_certs": ssl_verify}}
        conf_dict.update(
            {"globals": {"cache": {"lock_dir": "./locks", "tile_lock_dir": "./locks"}, "tiles": {"expires_hours": 0}}}
        )
    except Exception as e:
        logger.error(e)
        raise Exception(f"Unable to load a mapproxy configuration for slug {slug}")

    return conf_dict


def get_footprint_layer_name(slug):
    footprint_layer_name = f"{slug}-footprint"
    return footprint_layer_name


def get_mapproxy_metadata_url(slug):
    metadata_url = f"{settings.SITE_URL.rstrip('/')}/map/{slug}/service"
    return metadata_url


def get_mapproxy_footprint_url(slug):
    footprint_url = f"{settings.SITE_URL.rstrip('/')}/map/{slug}/wmts/{get_footprint_layer_name(slug)}/default/{{z}}/{{x}}/{{y}}.png"  # NOQA
    return footprint_url


def add_restricted_regions_to_config(
    base_config: dict, config: dict, slug: str, user: User = None
) -> Tuple[dict, dict]:
    from eventkit_cloud.jobs.models import DataProvider, RegionalPolicy

    config["sources"]["default"]["coverage"] = {
        "clip": True,
        "difference": [{"bbox": [-180, -90, 180, 90], "srs": "EPSG:4326"}],
    }
    providers = [get_cached_model(model=DataProvider, prop="slug", value=slug)]
    for policy in RegionalPolicy.objects.filter(providers__in=providers).prefetch_related("justifications"):
        # If no user no need to set up regional policy.
        if user and not get_valid_regional_justification(policy, user):
            config["sources"]["default"]["coverage"]["difference"].append(
                {"bbox": GEOSGeometry(policy.region.the_geom).extent, "srs": "EPSG:4326"}
            )
            for current_cache in base_config.get("caches", {}):
                base_config["caches"][current_cache]["disable_storage"] = True

    return base_config, config
