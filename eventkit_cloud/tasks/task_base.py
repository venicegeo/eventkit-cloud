import os

from audit_logging.celery_support import UserDetailsBase
from celery.utils.log import get_task_logger
from django.core.cache import caches
from django.conf import settings

logger = get_task_logger(__name__)


class EventKitBaseTask(UserDetailsBase):

    name = "EventKitBaseTask"

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        # This will only run in the PCF environment to shut down unused workers.
        super(EventKitBaseTask, self).after_return(status, retval, task_id, args, kwargs, einfo)
        pcf_scaling = os.getenv("PCF_SCALING", False)
        if pcf_scaling:
            from eventkit_cloud.tasks.util_tasks import pcf_shutdown_celery_workers

            queue_type, hostname = self.request.hostname.split("@")

            # In our current setup the queue name always mirrors the routing_key, if this changes this logic will break.
            queue_name = self.request.delivery_info["routing_key"]
            logger.info(f"{self.name} has completed, sending pcf_shutdown_celery_workers task.")
            pcf_shutdown_celery_workers.s(queue_name, queue_type, hostname).apply_async(
                queue=queue_name, routing_key=queue_name
            )


class LockingTask(UserDetailsBase):
    """
    Base task with lock to prevent multiple execution of tasks with ETA.
    It happens with multiple workers for tasks with any delay (countdown, ETA). Its a bug
    https://github.com/celery/kombu/issues/337.
    You may override cache backend by setting `CELERY_TASK_LOCK_CACHE` in your Django settings file.

    This task can also be used to ensure that a task isn't running at the same time as another task by specifying,
    a lock_key in the task arguments.  If the lock_key is present but unavailable the task will be tried again later.
    """

    cache = caches[getattr(settings, "CELERY_TASK_LOCK_CACHE", "default")]
    lock_expiration = 60 * 60 * 12  # 12 Hours
    lock_key = None
    max_retries = None

    def get_lock_key(self):
        """
        Unique string for task as lock key
        """
        return "TaskLock_%s_%s_%s" % (
            self.__class__.__name__,
            self.request.id,
            self.request.retries,
        )

    def acquire_lock(self, lock_key, value="True"):
        """
        Set lock.
        :param lock_key: Location to store lock.
        :param value: Some value to store for audit.
        :return:
        """
        result = False
        self.lock_key = lock_key
        try:
            result = self.cache.add(self.lock_key, value, self.lock_expiration)
            logger.debug("Acquiring {0} key: {1}".format(self.lock_key, "succeed" if result else "failed"))
        finally:
            return result

    def __call__(self, *args, **kwargs):
        """
        Checking for lock existence then call otherwise re-queue
        """
        retry = False
        logger.debug("enter __call__ for {0}".format(self.request.id))
        lock_key = kwargs.get("locking_task_key")
        worker = kwargs.get("worker")

        if lock_key:
            retry = True
        else:
            lock_key = self.get_lock_key()

        if self.acquire_lock(lock_key=lock_key, value=self.request.id):
            logger.debug("Task {0} started.".format(self.request.id))
            logger.debug("exit __call__ for {0}".format(self.request.id))
            result = super(LockingTask, self).__call__(*args, **kwargs)
            self.release_lock()
            return result
        else:
            if retry:
                logger.warn("Task {0} waiting for lock {1} to be free.".format(self.request.id, lock_key))
                if worker:
                    self.apply_async(args=args, kwargs=kwargs)
                else:
                    self.delay(*args, **kwargs)
            else:
                logger.info("Task {0} skipped due to lock".format(self.request.id))

    def release_lock(self):
        """
        Normally we would release the lock in an after_return method but there appears to be some issue with
        subclassing, https://github.com/celery/celery/issues/5666.
        """
        logger.debug(f"Task {self.request.id} releasing lock: {self.lock_key}")
        self.cache.delete(self.lock_key)
