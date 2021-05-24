import logging
import os
from typing import Union

import yaml
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

logger = logging.getLogger()


def get_provider_image_dir(provider_uid):
    """
    Get the directory where images for a specified provider will be stored for download.

    :param provider_uid: uid from the DataProvider model.
    """
    return os.path.join(settings.IMAGES_STAGING.rstrip("\/"), "providers", str(provider_uid))


def get_provider_thumbnail_name(provider_slug):
    """
    Get a short identifier for thumbnail images for a provider.

    :param provider_slug: slug (or identifier) for the specified DataProvider
    """
    return f"{provider_slug}_thmb"


def get_provider_image_download_dir(provider_uid):
    """
    Get the download path for images of a specified provider.

    :param provider_uid: uid from the DataProvider model.
    """
    return os.path.join(settings.IMAGES_DOWNLOAD_ROOT.rstrip("\/"), "providers", str(provider_uid))


def get_provider_image_download_path(provider_uid):
    """
    Get the download path for images of a specified provider.

    :param provider_uid: uid from the DataProvider model.
    """
    return os.path.join(
        settings.EXPORT_MEDIA_ROOT.rstrip("\/"),
        "images",
        "providers",
        str(provider_uid),
    )


def get_valid_regional_justification(regional_policy, user: User):
    """
    Checks if a user has an active regional justification for a specific regional policy.
    Returns the regional justification or None.
    """
    regional_justifications = regional_policy.justifications.filter(user=user)

    if not regional_justifications:
        return None

    regional_justification = regional_justifications.latest("created_at")

    # If a timeout was set in the environment, use that timeout.
    if settings.REGIONAL_JUSTIFICATION_TIMEOUT_DAYS:
        timeout_seconds = settings.REGIONAL_JUSTIFICATION_TIMEOUT_DAYS * 3600 * 24
        seconds_since_created = (timezone.now() - regional_justification.created_at).total_seconds()
        if seconds_since_created > timeout_seconds:
            return None
    else:
        # If there's no timeout set, use the last login instead.
        if regional_justification.created_at < user.last_login:
            return None

    return regional_justification


def clean_config(config: str, return_dict: bool = False) -> Union[str, dict]:
    """
    Used to remove adhoc service related values from the configuration.
    :param config: A yaml structured string.
    :param return_dict: True if wishing to return config as dictionary.
    :return: A yaml as a str.
    """
    service_keys = [
        "cert_info",
        "cert_cred",
        "concurrency",
        "max_repeat",
        "overpass_query",
        "max_data_size",
        "pbf_file",
        "tile_size",
    ]

    conf = yaml.safe_load(config) or dict()

    for service_key in service_keys:
        conf.pop(service_key, None)
    if return_dict:
        return conf
    return yaml.dump(conf)
