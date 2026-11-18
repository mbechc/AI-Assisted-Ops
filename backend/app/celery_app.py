import logging
import os

from celery import Celery
from celery.signals import task_failure, task_postrun, task_prerun, worker_ready

from app.config import get_settings
from app.logging_setup import configure_logging

configure_logging()

celery = Celery("aiops", broker=get_settings().redis_url)
celery.conf.update(
    task_default_queue="run.default",
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
    task_time_limit=6 * 3600,
    worker_hijack_root_logger=False,
)
app = celery
log = logging.getLogger(__name__)


@worker_ready.connect
def _on_ready(sender=None, **_):
    from app.workers import start_heartbeat
    queues = sorted(q.name for q in sender.app.amqp.queues.consume_from.values()) if sender else []
    start_heartbeat(queues, getattr(sender, "concurrency", None))
    try:
        from app.framework.runner import fail_orphaned_runs
        fail_orphaned_runs(os.environ.get("WORKER_NAME", "default"))
    except Exception:  # noqa: BLE001
        log.exception("could not clean up runs orphaned by the previous worker")


@task_prerun.connect
def _on_prerun(task_id=None, task=None, args=None, **_):
    from app.workers import mark_active
    try:
        mark_active(task_id, task.name, args)
    except Exception:  # noqa: BLE001
        log.debug("heartbeat bookkeeping failed", exc_info=True)


@task_postrun.connect
def _on_postrun(task_id=None, state=None, **_):
    from app.workers import mark_done
    try:
        mark_done(task_id, failed=state == "FAILURE")
    except Exception:  # noqa: BLE001
        log.debug("heartbeat bookkeeping failed", exc_info=True)


@task_failure.connect
def _on_failure(task_id=None, exception=None, **_):
    log.error("task %s failed: %s", task_id, exception)


@celery.task(name="aiops.run_task")
def run_task(run_id: int) -> None:
    from app.framework.runner import execute_run
    execute_run(run_id)


@celery.task(name="aiops.execute_items")
def execute_items_task(task_key: str, ids: list[int]) -> None:
    from app.framework.runner import execute_items
    execute_items(task_key, ids)


@celery.task(name="aiops.dispatch")
def dispatch_task(task_key: str | None = None) -> None:
    from app.framework.runner import dispatch_approved
    dispatch_approved(task_key)
