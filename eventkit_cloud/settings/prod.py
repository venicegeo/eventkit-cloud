# -*- coding: utf-8 -*-


import dj_database_url
import os
import json
import socket

from eventkit_cloud.settings.celery import *  # noqa
from eventkit_cloud.settings.celery import INSTALLED_APPS
from eventkit_cloud.settings.celery import MIDDLEWARE
from eventkit_cloud.settings.celery import is_true

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

INSTALLED_APPS += ("django_celery_beat", "django_filters")

# Project apps
INSTALLED_APPS += (
    "eventkit_cloud.core",
    "eventkit_cloud.auth",
    "eventkit_cloud.jobs",
    "eventkit_cloud.tasks",
    "eventkit_cloud.api",
    "eventkit_cloud.ui",
    "eventkit_cloud.user_requests",
    "eventkit_cloud.utils",
    "eventkit_cloud",
    "notifications",
)

LOGIN_URL = "/login"
LOGOUT_URL = "/logout"

EXPORT_TASKS = {
    "shp": "eventkit_cloud.tasks.export_tasks.shp_export_task",
    "obf": "eventkit_cloud.tasks.export_tasks.ObfExportTask",
    "sqlite": "eventkit_cloud.tasks.export_tasks.sqlite_export_task",
    "kml": "eventkit_cloud.tasks.export_tasks.kml_export_task",
    "thematic": "eventkit_cloud.tasks.export_tasks.ThematicLayersExportTask",
    "gpkg": "eventkit_cloud.tasks.export_tasks.geopackage_export_task",
}


# where exports are staged for processing
EXPORT_STAGING_ROOT = None
TILE_CACHE_DIR = None
if os.getenv("VCAP_SERVICES"):
    for service, listings in json.loads(os.getenv("VCAP_SERVICES")).items():
        if "nfs" in service:
            try:
                EXPORT_STAGING_ROOT = os.path.join(listings[0]["volume_mounts"][0]["container_dir"], "eventkit_stage")
                TILE_CACHE_DIR = os.path.join(listings[0]["volume_mounts"][0]["container_dir"], "tile_cache")
            except (KeyError, TypeError) as e:
                print(e)
                continue
if not EXPORT_STAGING_ROOT:
    EXPORT_STAGING_ROOT = os.getenv("EXPORT_STAGING_ROOT", "/var/lib/eventkit/exports_stage/")
if not TILE_CACHE_DIR:
    TILE_CACHE_DIR = os.getenv("TILE_CACHE_DIR", "/var/lib/eventkit/tile_cache/")

# where map image snapshots are stored (e.g. thumbnails)
IMAGES_STAGING = os.path.join(EXPORT_STAGING_ROOT, "images")

# where export run files to be added to every datapack are stored
EXPORT_RUN_FILES = os.path.join(EXPORT_STAGING_ROOT, "export_run_files")

# where export run files can be downloaded
EXPORT_RUN_FILES_DOWNLOAD = os.getenv("EXPORT_RUN_FILES_DOWNLOAD", "/export_run_files/")

# where exports are stored for public download
EXPORT_DOWNLOAD_ROOT = os.getenv("EXPORT_DOWNLOAD_ROOT", "/var/lib/eventkit/exports_download/")

IMAGES_DOWNLOAD_ROOT = os.path.join(EXPORT_DOWNLOAD_ROOT, "images")

# the root url for export downloads
EXPORT_MEDIA_ROOT = os.getenv("EXPORT_MEDIA_ROOT", "/downloads/")

# url to overpass api endpoint
# OVERPASS_API_URL = 'http://cloud.eventkit.test/overpass-api/interpreter'
OVERPASS_API_URL = os.getenv("OVERPASS_API_URL", "http://overpass-api.de/api/interpreter")
GEOCODING_API_URL = os.getenv("GEOCODING_API_URL", "http://api.geonames.org/searchJSON")
GEOCODING_API_TYPE = os.getenv("GEOCODING_API_TYPE", "GEONAMES")
REVERSE_GEOCODING_API_URL = os.getenv("REVERSE_GEOCODING_API_URL", None)
REVERSE_GEOCODING_API_TYPE = os.getenv("REVERSE_GEOCODING_API_TYPE", GEOCODING_API_TYPE)
GEOCODING_UPDATE_URL = os.getenv("GEOCODING_UPDATE_URL", None)
GEOCODING_AUTH_URL = os.getenv("GEOCODING_AUTH_URL", None)
GEOCODING_AUTH_CERT = os.getenv("GEOCODING_AUTH_CERT", None)
CONVERT_API_URL = os.getenv("CONVERT_API_URL", None)

# zoom extents of reverse geocode point result (in degrees)
REVERSE_GEOCODE_ZOOM = 0.1

"""
Maximum extent of a Job
max of (latmax-latmin) * (lonmax-lonmin)
"""
JOB_MAX_EXTENT = int(os.getenv("JOB_MAX_EXTENT", "10000"))  # default export max extent in sq km

# maximum number of runs to hold for each export
EXPORT_MAX_RUNS = 1

HOSTNAME = os.getenv("HOSTNAME", socket.gethostname())
if os.getenv("VCAP_APPLICATION"):
    env = json.loads(os.getenv("VCAP_APPLICATION"))
    if env["application_uris"]:
        HOSTNAME = os.getenv("HOSTNAME", env["application_uris"][0])
SITE_NAME = os.getenv("SITE_NAME", HOSTNAME)
if SITE_NAME == "":
    SITE_NAME = "localhost"
if os.getenv("VCAP_APPLICATION"):
    SITE_URL = os.getenv("SITE_URL", "https://{0}".format(SITE_NAME))
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
else:
    SITE_URL = os.getenv("SITE_URL", "http://{0}".format(SITE_NAME))
SITE_ID = 1

"""
Admin email address
which receives task error notifications.
"""
TASK_ERROR_EMAIL = os.getenv("TASK_ERROR_EMAIL", None)
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", None)
EMAIL_HOST = os.getenv("EMAIL_HOST", None)
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", None)
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", None)
USE_EMAIL = is_true(os.getenv("USE_EMAIL", False))

if USE_EMAIL:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

EMAIL_USE_TLS = is_true(os.getenv("EMAIL_USE_TLS", "true"))

"""
Overpass Element limit
Sets the max ram allowed for overpass query

http://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_QL#Element_limit_.28maxsize.29
"""

OVERPASS_MAX_SIZE = os.getenv("OVERPASS_MAX_SIZE", 2147483648)  # 2GB

"""
Overpass timeout setting

Sets request timeout for overpass queries.

http://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_QL#timeout
"""

OVERPASS_TIMEOUT = os.getenv("OVERPASS_TIMEOUT", 1600)  # query timeout in seconds

# Authentication Settings

AUTHENTICATION_BACKENDS = tuple()

if os.getenv("OAUTH_AUTHORIZATION_URL"):
    OAUTH_NAME = os.getenv("OAUTH_NAME", "OAUTH")
    OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID")
    OAUTH_CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET")
    OAUTH_AUTHORIZATION_URL = os.getenv("OAUTH_AUTHORIZATION_URL")
    OAUTH_LOGOUT_URL = os.getenv("OAUTH_LOGOUT_URL")
    OAUTH_TOKEN_URL = os.getenv("OAUTH_TOKEN_URL")
    OAUTH_TOKEN_KEY = os.getenv("OAUTH_TOKEN_KEY", "access_token")
    OAUTH_RESPONSE_TYPE = os.getenv("OAUTH_RESPONSE_TYPE", "code")
    OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI")
    OAUTH_SCOPE = os.getenv("OAUTH_SCOPE")

    # The OAuth profile needs to map to the User model.

    # The required fields are:
    # identification, username, email, commonname
    # The optional fields are:
    # first_name, last_name

    # Example:
    # OAUTH_PROFILE_SCHEMA = {"identification": "ID", "username": "username", "email": "email", "first_name":
    # "firstname"...}
    OAUTH_PROFILE_SCHEMA = os.getenv("OAUTH_PROFILE_SCHEMA")
    OAUTH_PROFILE_URL = os.getenv("OAUTH_PROFILE_URL")

if os.getenv("LDAP_SERVER_URI"):
    import ldap
    from django_auth_ldap.config import LDAPSearch

    AUTH_LDAP_SERVER_URI = os.getenv("LDAP_SERVER_URI")
    AUTH_LDAP_BIND_PASSWORD = os.getenv("LDAP_BIND_PASSWORD")
    AUTH_LDAP_USER_DN_TEMPLATE = os.getenv("LDAP_USER_DN_TEMPLATE")
    LDAP_SEARCH_DN = os.getenv("LDAP_SEARCH_DN")
    AUTH_LDAP_USER = os.getenv("LDAP_USER")
    AUTH_LDAP_BIND_DN = os.getenv("LDAP_BIND_DN")
    AUTHENTICATION_BACKENDS += ("django_auth_ldap.backend.LDAPBackend",)

    AUTH_LDAP_USER_ATTR_MAP = {
        "first_name": "givenName",
        "last_name": "sn",
        "email": "mail",
    }
    AUTH_LDAP_USER_SEARCH = LDAPSearch(LDAP_SEARCH_DN, ldap.SCOPE_SUBTREE, AUTH_LDAP_USER)

DJANGO_MODEL_LOGIN = os.getenv("DJANGO_MODEL_LOGIN")
AUTHENTICATION_BACKENDS += ("django.contrib.auth.backends.ModelBackend",)

# Set debug to True for development
DEBUG = is_true(os.getenv("DEBUG", False))

ALLOWED_HOSTS = [HOSTNAME, SITE_NAME]

LOGGING_OUTPUT_ENABLED = DEBUG
LOGGING_LOG_SQL = DEBUG

INSTALLED_APPS += (
    "django_extensions",
    "audit_logging",
)

MIDDLEWARE += ["audit_logging.middleware.UserDetailsMiddleware"]

AUDIT_MODELS = [
    ("eventkit_cloud.tasks.models.ExportRun", "ExportRun"),
    ("eventkit_cloud.tasks.models.DataProviderTaskRecord", "DataProviderTaskRecord"),
    ("eventkit_cloud.tasks.models.ExportTaskRecord", "ExportTaskRecord"),
]

DATABASES = {}

if os.getenv("VCAP_SERVICES"):
    if os.getenv("DATABASE_URL"):
        DATABASES = {"default": dj_database_url.config()}
    if not DATABASES:
        for service, listings in json.loads(os.getenv("VCAP_SERVICES")).items():
            try:
                if ("pg_95" in service) or ("postgres" in service):
                    DATABASES["default"] = dj_database_url.config(default=listings[0]["credentials"]["uri"])
                    DATABASES["default"]["CONN_MAX_AGE"] = 180
            except (KeyError, TypeError) as e:
                print(("Could not configure information for service: {0}".format(service)))
                print(e)
                continue
            if DATABASES:
                break
else:
    DATABASES["default"] = dj_database_url.config(
        default="postgres://eventkit:eventkit_exports@localhost:5432/eventkit_exports"
    )

DATABASES["default"]["ENGINE"] = "django.contrib.gis.db.backends.postgis"

DATABASES["default"]["OPTIONS"] = {"options": "-c search_path=exports,public"}

if os.getenv("FEATURE_DATABASE_URL"):
    DATABASES["feature_data"] = dj_database_url.parse(os.getenv("FEATURE_DATABASE_URL"))
else:
    DATABASES["feature_data"] = DATABASES["default"]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": ["api/templates/", "ui/templates", "tasks/templates", "ui/static/ui/js"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
            "debug": DEBUG,
        },
    },
]

if os.getenv("MEMCACHED"):
    CACHES = {
        "default": {"BACKEND": "eventkit_cloud.utils.fallback_cache.FallbackCache"},
        "primary_cache": {
            "BACKEND": "django.core.cache.backends.memcached.MemcachedCache",
            "LOCATION": os.getenv("MEMCACHED"),
            # "OPTIONS": {"server_max_value_length": os.getenv("MEMCACHED_SERVER_MAX_VALUE_LENGTH", 1024 * 1024 * 10)},
        },
        "fallback_cache": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "eventkit_cache"},
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "eventkit_cache"}}

# session settings
SESSION_COOKIE_NAME = "eventkit_exports_sessionid"
SESSION_COOKIE_DOMAIN = os.getenv("SESSION_COOKIE_DOMAIN", SITE_NAME)
SESSION_COOKIE_PATH = "/"
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_USER_LAST_ACTIVE_AT = "user_last_active_at"

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"
SERVE_ESTIMATES = is_true(os.getenv("SERVE_ESTIMATES", "true"))
DATAPACKS_DEFAULT_SHARED = is_true(os.getenv("DATAPACKS_DEFAULT_SHARED", "false"))
VERSION = os.getenv("VERSION", "")
UI_CONFIG = {
    "VERSION": VERSION,
    "CONTACT_URL": os.getenv("CONTACT_URL", "mailto:eventkit.team@gmail.com"),
    "LOGIN_DISCLAIMER": os.getenv("LOGIN_DISCLAIMER", ""),
    "BANNER_BACKGROUND_COLOR": os.getenv("BANNER_BACKGROUND_COLOR", ""),
    "BANNER_TEXT_COLOR": os.getenv("BANNER_TEXT_COLOR", ""),
    "BANNER_TEXT": os.getenv("BANNER_TEXT", ""),
    "BASEMAP_URL": os.getenv("BASEMAP_URL", "http://tile.openstreetmap.org/{z}/{x}/{y}.png"),
    "BASEMAP_COPYRIGHT": os.getenv("BASEMAP_COPYRIGHT", "© OpenStreetMap"),
    "MAX_DATAPACK_EXPIRATION_DAYS": os.getenv("MAX_DATAPACK_EXPIRATION_DAYS", "30"),
    "USER_GROUPS_PAGE_SIZE": os.getenv("USER_GROUPS_PAGE_SIZE", "20"),
    "DATAPACK_PAGE_SIZE": os.getenv("DATAPACK_PAGE_SIZE", "100"),
    "NOTIFICATIONS_PAGE_SIZE": os.getenv("NOTIFICATIONS_PAGE_SIZE", "10"),
    "SERVE_ESTIMATES": SERVE_ESTIMATES,
    "DATAPACKS_DEFAULT_SHARED": DATAPACKS_DEFAULT_SHARED,
    "MAX_UPLOAD_SIZE": os.getenv("MAX_UPLOAD_SIZE", 5),  # In MB
    "MATOMO": {
        "URL": os.getenv("MATOMO_URL", ""),
        "CUSTOM_DIM_ID": os.getenv("MATOMO_CUSTOM_DIM_ID", ""),
        "CUSTOM_VAR_NAME": os.getenv("MATOMO_CUSTOM_VAR_NAME", ""),
        "CUSTOM_VAR_ID": os.getenv("MATOMO_CUSTOM_VAR_ID", ""),
        "CUSTOM_VAR_SCOPE": os.getenv("MATOMO_CUSTOM_VAR_SCOPE", "page"),
        "SITE_ID": os.getenv("MATOMO_SITE_ID", ""),
        "APPNAME": os.getenv("MATOMO_APPNAME", "EventKit"),
    },
}


USE_S3 = is_true(os.getenv("USE_S3"))

if USE_S3:
    DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"

AWS_STORAGE_BUCKET_NAME = AWS_ACCESS_KEY_ID = AWS_SECRET_ACCESS_KEY = None
if os.getenv("VCAP_SERVICES"):
    for service, listings in json.loads(os.getenv("VCAP_SERVICES")).items():
        if "s3" in service.lower():
            try:
                AWS_STORAGE_BUCKET_NAME = listings[0]["credentials"]["bucket"]
                AWS_ACCESS_KEY_ID = listings[0]["credentials"]["access_key_id"]
                AWS_SECRET_ACCESS_KEY = listings[0]["credentials"]["secret_access_key"]
            except (KeyError, TypeError):
                continue
AWS_STORAGE_BUCKET_NAME = AWS_STORAGE_BUCKET_NAME or os.getenv("AWS_STORAGE_BUCKET_NAME")
AWS_ACCESS_KEY_ID = AWS_ACCESS_KEY_ID or os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = AWS_SECRET_ACCESS_KEY or os.getenv("AWS_SECRET_ACCESS_KEY")

# https://django-storages.readthedocs.io/en/latest/backends/amazon-S3.html#settings
AWS_DEFAULT_ACL = None


MAPPROXY_CONCURRENCY = os.getenv("MAPPROXY_CONCURRENCY", 1)
MAPPROXY_LOGS = {
    "requests": is_true(os.getenv("MAPPROXY_LOGS_REQUESTS")),
    "verbose": is_true(os.getenv("MAPPROXY_LOGS_VERBOSE")),
    "silent": is_true(os.getenv("MAPPROXY_LOGS_SILENT")),
}
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "formatters": {"simple": {"format": "[{asctime}] {module} - {levelname} - {message}", "style": "{"}},
    "root": {"handlers": ["console"], "propagate": True, "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "propagate": True, "level": os.getenv("DJANGO_LOG_LEVEL", "WARN")},
    },
}

# SSL_VERIFICATION should point to a CA certificate file (.pem), if not then REQUESTS_CA_BUNDLE should be set also.
# If wishing to disable verification (not recommended), set SSL_VERIFICATION to False.
ssl_verification_settings = os.getenv("SSL_VERIFICATION", "true")
if os.path.isfile(ssl_verification_settings):
    SSL_VERIFICATION = ssl_verification_settings
    if not os.getenv("REQUESTS_CA_BUNDLE"):
        os.environ["REQUESTS_CA_BUNDLE"] = SSL_VERIFICATION
else:
    SSL_VERIFICATION = is_true(ssl_verification_settings)

LAND_DATA_URL = os.getenv(
    "LAND_DATA_URL",
    "https://osmdata.openstreetmap.de/download/land-polygons-split-3857.zip",
)

AUTO_LOGOUT_COOKIE_NAME = "eventkit_auto_logout"

AUTO_LOGOUT_SECONDS = int(os.getenv("AUTO_LOGOUT_SECONDS", 0))
AUTO_LOGOUT_WARNING_AT_SECONDS_LEFT = int(os.getenv("AUTO_LOGOUT_WARNING_AT_SECONDS_LEFT", 5 * 60))
if AUTO_LOGOUT_SECONDS:
    MIDDLEWARE += ["eventkit_cloud.auth.auth.auto_logout"]

DJANGO_NOTIFICATIONS_CONFIG = {"SOFT_DELETE": True}

ROCKETCHAT_NOTIFICATIONS = json.loads(os.getenv("ROCKETCHAT_NOTIFICATIONS", "{}"))

REGIONAL_JUSTIFICATION_TIMEOUT_DAYS = int(os.getenv("REGIONAL_JUSTIFICATION_TIMEOUT_DAYS", 0))

OSM_MAX_TMPFILE_SIZE = os.getenv("OSM_MAX_TMPFILE_SIZE", "100")
OSM_USE_CUSTOM_INDEXING = os.getenv("OSM_USE_CUSTOM_INDEXING", "NO")
