import os

from celery import Celery
from celery.signals import setup_logging, worker_process_shutdown, worker_ready
from kombu import Exchange, Queue
from prometheus_client import CollectorRegistry, multiprocess, start_http_server

from core.config import get_settings
from core.logging import configure_logging

settings = get_settings()
_worker_metrics_started = False

celery_app = Celery(
    "abrio-gateway",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "infra.workers.dispatch_tasks",
    ],
)

sms_exchange = Exchange(
    "sms",
    type="direct",
    durable=True,
)

celery_app.conf.update(
    task_queues=(
        Queue(
            "sms.express",
            exchange=sms_exchange,
            routing_key="sms.express",
            durable=True,
        ),
        Queue(
            "sms.normal",
            exchange=sms_exchange,
            routing_key="sms.normal",
            durable=True,
        ),
    ),
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_default_delivery_mode="persistent",
    # Let the database relay own publication retries.
    task_publish_retry=False,
    # Detect broker rejection or inability to persist the publication.
    broker_transport_options={
        "confirm_publish": True,
    },
    worker_hijack_root_logger=False,
)


@setup_logging.connect
def configure_celery_logging(**_: object) -> None:
    configure_logging(settings)


@worker_ready.connect
def start_worker_metrics_server(**_: object) -> None:
    global _worker_metrics_started
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    if _worker_metrics_started:
        return

    port = int(os.environ.get("METRICS_PORT", "9102"))
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    start_http_server(
        port,
        addr="0.0.0.0",
        registry=registry,
    )
    _worker_metrics_started = True


@worker_process_shutdown.connect
def mark_worker_process_dead(pid: int | None = None, **_: object) -> None:
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return

    multiprocess.mark_process_dead(pid or os.getpid())
