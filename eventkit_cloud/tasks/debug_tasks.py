import time
from logging import getLogger

import celery

from eventkit_cloud.celery import app

logger = getLogger(__name__)


@app.task(name="TestChain", bind=True)
def test_chain(self, subtask_queuename=None):
    """Returns the AsyncResult of a chain of 4 test_task queued on @subtask_queuename."""
    assert subtask_queuename is not None

    logger.info("TestChain subtask_queuename: {}".format(subtask_queuename))
    delivery_info = self.request.delivery_info
    logger.info("TestChain delivery_info: {}".format(delivery_info))

    test_tasks = [test_task.s(0).set(queue=subtask_queuename)]
    test_tasks.extend([test_task.s().set(queue=subtask_queuename) for i in range(3)])
    tc = celery.chain(test_tasks)
    res = tc.delay()

    return res


@app.task(name="TestTask", bind=True)
def test_task(self, a):
    time.sleep(1)
    r = a + 1

    delivery_info = self.request.delivery_info
    logger.info("TestTask delivery_info: {}".format(delivery_info))
    logger.info("TestTask: {}".format(r))
    return r


@app.task(name="TestFailingTask", bind=True)
def test_failing_task(self, a):
    raise Exception("This exception is not a mistake")


@app.task(name="TestErrBack")
def test_errback(msg="test_errback"):
    logger.info("--- {} executing ---".format(msg))
