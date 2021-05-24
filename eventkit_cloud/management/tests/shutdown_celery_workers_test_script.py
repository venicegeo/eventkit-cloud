import os
import sys

from celery.result import AsyncResult

import django
from django.core.management import call_command

from eventkit_cloud.tasks import test_chain
from eventkit_cloud.celery import app

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "eventkit_cloud.settings.prod")
django.setup()

# CELERY_ALWAYS_EAGER = False


def chain_success(chain_async_result):
    """Checks that each task for the chain producing @chain_result completed successfully.
    Returns True if all tasks are successful, False otherwise.
    """
    print("Waiting on chain.ready()")
    # Make sure the chain has completed
    result = chain_async_result.get()
    msgs = []

    def traverse_chain_result(result):
        result_id, remaining_chain = result[0]
        link_result = AsyncResult(result_id)
        msgs.append("{}: {}".format(result_id, link_result.status))

        if type(remaining_chain) == list:
            remaining_links_completed = traverse_chain_result(remaining_chain)
        else:
            remaining_links_completed = []

        statuses = [link_result.status] + remaining_links_completed
        return statuses

    statuses = traverse_chain_result(result)
    if all([s == "SUCCESS" for s in statuses]):
        return True
    else:
        print("\n".join(msgs))
        return False


def test_shutdown_celery_workers_mgmt_cmd():
    """Checks that 'shutdown_celery_workers' allows previous tasks to finish, but prevents additional processing
    then stops the celery workers.
    """
    # Ensure celery container is running
    if app.control.inspect().ping() is None:
        return (100, "No worker nodes found running prior to test")

    worker_nodename = [n for n in list(app.control.inspect().ping().keys()) if "worker" in n][0]
    worker_hostname = worker_nodename.split("@")[1]

    # Fire off a number of tasks & wait for them to be picked up
    tc1 = test_chain.apply_async(kwargs={"subtask_queuename": worker_hostname}, expires=6)
    tc1.get()

    # Instruct system to finish current tasks
    call_command("finish_worker_tasks")

    # fire off more tasks
    tc2 = test_chain.apply_async(kwargs={"subtask_queuename": worker_hostname}, expires=6)

    if tc1.status != "SUCCESS":
        return (100, 'Tasks started before "finish_worker_tasks", status "{}" != "SUCCESS"'.format(tc1.status))
    if not chain_success(tc1):
        return (100, 'Tasks started before "finish_worker_tasks", did not all complete successfully')
    if tc2.status != "PENDING":
        return (100, 'Tasks started after "finish_worker_tasks", status {} != "PENDING"'.format(tc2.status))

    # Invoke shutdown_celery_workers
    call_command("shutdown_celery_workers")

    # Check that the celery workers are all stopped
    if app.control.inspect().ping() is not None:
        return (100, "Worker nodes still running after test")

    # There should be one test_chain task on the celery queue that's left after "finish_worker_tasks" call.
    n_purged_tasks = app.control.purge()
    if n_purged_tasks != 1:
        return (100, "Number of pending tasks purged, {} != 4".format(n_purged_tasks))

    return (0, "Ok")


if __name__ == "__main__":
    n_preexisting_tasks = app.control.purge()
    if n_preexisting_tasks != 0:
        return_code, msg = (100, "Found {} preexisting tasks, please clean up queue before executing this test")
    else:
        return_code, msg = test_shutdown_celery_workers_mgmt_cmd()
    print(msg)
    sys.exit(return_code)
