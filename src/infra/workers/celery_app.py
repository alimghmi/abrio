from celery import Celery
from celery.signals import setup_logging
from kombu import Exchange, Queue

from core.config import get_settings
from core.logging import configure_logging

settings = get_settings()

celery_app = Celery(
    "abrio",
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
