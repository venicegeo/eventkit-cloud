#!/bin/bash

rm -rf celerybeat.pid & \
celery -A eventkit_cloud beat --loglevel=$LOG_LEVEL & \
celery -A eventkit_cloud worker --concurrency=1 --loglevel=$LOG_LEVEL -n scale@%h -Q scale
