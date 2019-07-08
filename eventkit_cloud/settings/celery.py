# -*- coding: utf-8 -*-
import json
import os
import socket

from celery.schedules import crontab
from kombu import Exchange, Queue

from eventkit_cloud.celery import app
from eventkit_cloud.settings.contrib import *  # NOQA

# Celery config
CELERY_TRACK_STARTED = True

"""
 IMPORTANT

 Don't propagate exceptions in the celery chord header to the finalize task.
 If exceptions are thrown in the chord header then allow the
 finalize task to collect the results and update the overall run state.

"""
# CELERY_CHORD_PROPAGATES = False

CELERYD_PREFETCH_MULTIPLIER = 1
CELERYBEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'django-db')

# Pickle used to be the default, and accepting pickled content is a security concern.  Using the new default json,
# causes a circular reference error, that will need to be resolved.
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
# configure periodic task

BEAT_SCHEDULE = {
    'expire-runs': {
        'task': 'Expire Runs',
        'schedule': crontab(minute='0', hour='0', day_of_week='*')
    },
    'provider-statuses': {
        'task': 'Check Provider Availability',
        'schedule': crontab(minute='*/{}'.format(os.getenv('PROVIDER_CHECK_INTERVAL', '30')))
    },
    'clean-up-queues': {
        'task': 'Clean Up Queues',
        'schedule': crontab(minute='0', hour='0', day_of_week='*')
    },
}

PCF_SCALING = os.getenv("PCF_SCALING", False)
if PCF_SCALING:
    BEAT_SCHEDULE.update({
        'pcf-scale-celery': {
            'task': 'PCF Scale Celery',
            'schedule': 60.0,
            'kwargs' : {"max_instances": int(os.getenv("CELERY_INSTANCES", 3))},
            'options': {'priority': 90,
                        'queue': "scale".format(socket.gethostname()),
                        'routing_key': "scale".format(socket.gethostname())}
        },
    })
    # memory and disk in MB
    pcf_celery_defaults = {"default": {"memory": 1024, "disk": 2048, "concurrency": 2},
                           "large": {"memory": 3072, "disk": 6144, "concurrency": 1}}
    PCF_CELERY_SETTINGS = os.getenv(json.loads("PCF_CELERY_SETTINGS", pcf_celery_defaults))

CELERY_GROUP_NAME = os.getenv("CELERY_GROUP_NAME", socket.gethostname())

app.conf.beat_schedule = BEAT_SCHEDULE

CELERYD_USER = CELERYD_GROUP = 'eventkit'
if os.getenv("VCAP_SERVICES"):
    CELERYD_USER = CELERYD_GROUP = "vcap"
CELERYD_USER = os.getenv("CELERYD_USER", CELERYD_USER)
CELERYD_GROUP = os.getenv("CELERYD_GROUP", CELERYD_GROUP)

BROKER_URL = None
if os.getenv("VCAP_SERVICES"):
    for service, listings in json.loads(os.getenv("VCAP_SERVICES")).items():
        try:
            if 'rabbitmq' in service:
                BROKER_URL = listings[0]['credentials']['protocols']['amqp']['uri']
            if 'cloudamqp' in service:
                BROKER_URL = listings[0]['credentials']['uri']
        except KeyError as TypeError:
            continue
        if BROKER_URL:
            break
if not BROKER_URL:
    BROKER_URL = os.environ.get('BROKER_URL', 'amqp://guest:guest@localhost:5672//')

BROKER_API_URL = None
if os.getenv("VCAP_SERVICES"):
    for service, listings in json.loads(os.getenv("VCAP_SERVICES")).items():
        try:
            if 'rabbitmq' in service:
                BROKER_API_URL = listings[0]['credentials']['http_api_uri']
            if 'cloudamqp' in service:
                BROKER_API_URL = listings[0]['credentials']['http_api_uri']
        except KeyError as TypeError:
            continue
        if BROKER_API_URL:
            break
if not BROKER_API_URL:
    BROKER_API_URL = os.environ.get('BROKER_API_URL', 'http://guest:guest@localhost:15672/api/')
