# -*- coding: utf-8 -*-

from django.core.cache import cache
from django.apps import apps as django_apps

import logging

logger = logging.getLogger(__file__)

default_app_config = "eventkit_cloud.tasks.apps.EventKitTasks"

DEFAULT_CACHE_EXPIRATION = 86400  # expire in a day


def get_cache_key(obj=None, attribute=None, uid=None, model_name=None):
    """
    A way to store values in the cache ideally models would use their own implementation of this, but this could
    be called directly to prevent the need to call the model to update the state.

    Example
    :param obj: A string representing a model name (i.e. ExportTaskRecord)
    :param attribute: The models attribute.
    :param uid: An optional uid if a specific object isn't passed.
    :param model_name: An optional model_name if an object isn't passed.
    :return:
    """
    obj_uid = uid
    if obj:
        obj_uid = obj.uid
        model_name = type(obj).__name__
        model = type(obj)
    else:
        try:
            model = django_apps.get_model("tasks", model_name)
        except LookupError:
            logger.error("There is no such model {0}".format(model_name))
            raise
    if not obj_uid:
        raise Exception("Cannot cache a state without a uid.")
    if not hasattr(model, attribute):
        raise Exception("{} does not have the attribute {}".format(model, attribute))
    cache_key = "{}.{}.{}".format(model_name, str(obj_uid), attribute)
    return cache_key


def set_cache_value(
    obj=None,
    uid=None,
    attribute=None,
    value=None,
    model_name=None,
    expiration=DEFAULT_CACHE_EXPIRATION,
):
    return cache.set(
        get_cache_key(obj=obj, attribute=attribute, uid=str(uid), model_name=model_name),
        value,
        timeout=expiration,
    )


def get_cache_value(obj=None, uid=None, attribute=None, model_name=None, default=None):
    return cache.get(
        get_cache_key(obj=obj, attribute=attribute, uid=str(uid), model_name=model_name),
        default,
    )
