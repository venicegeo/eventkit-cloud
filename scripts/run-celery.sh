#!/bin/bash

celery -A eventkit_cloud worker --concurrency=$RUNS_CONCURRENCY --loglevel=$LOG_LEVEL -n runs@%h -Q runs & \
celery -A eventkit_cloud worker --concurrency=$CONCURRENCY --loglevel=$LOG_LEVEL -n worker@%h -Q $CELERY_GROUP_NAME & \
celery -A eventkit_cloud worker --concurrency=1 --loglevel=$LOG_LEVEL -n celery@%h -Q celery & \
celery -A eventkit_cloud worker --concurrency=1 --loglevel=$LOG_LEVEL -n priority@%h -Q $CELERY_GROUP_NAME.priority,$HOSTNAME.priority & \
celery -A eventkit_cloud worker --concurrency=$OSM_CONCURRENCY --loglevel=$LOG_LEVEL -n large@%h -Q $CELERY_GROUP_NAME.large
