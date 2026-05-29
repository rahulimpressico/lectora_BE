"""Publish job messages to Azure Service Bus."""
import asyncio
import json
import logging
import threading

from azure.servicebus import ServiceBusClient, ServiceBusMessage

from lectora_backend.config import settings


logger = logging.getLogger(__name__)


class QueuePublisher:
    """Reusable Service Bus publisher.

    The ServiceBusClient is kept alive for the process lifetime — do NOT wrap
    ``self._client`` in a ``with`` block, which would close the connection after
    the first use.  Only the *sender* is used as a short-lived context manager.
    """

    def __init__(self) -> None:
        self._client = ServiceBusClient.from_connection_string(
            conn_str=settings.service_bus_connection_string
        )
        self._queue_name = settings.queue_name

    def _send_message(self, message_body: str) -> None:
        # Use the sender as a context manager (opens/closes per send) while
        # keeping the parent client open for subsequent calls.
        with self._client.get_queue_sender(queue_name=self._queue_name) as sender:
            sender.send_messages(ServiceBusMessage(message_body))

    async def enqueue(self, job_id: str) -> None:
        message_body = json.dumps(
            {
                "jobId": job_id,
                "eventType": "JOB_CREATED",
            }
        )
        await asyncio.to_thread(self._send_message, message_body)
        logger.info("Enqueued job %s to queue %s", job_id, self._queue_name)


# ── Process-level singleton ────────────────────────────────────────────────────
# Creating a new ServiceBusClient per request opens a new TCP connection every
# time.  A singleton reuses the connection for the process lifetime.

_publisher: QueuePublisher | None = None
_publisher_lock = threading.Lock()


def get_queue_publisher() -> QueuePublisher:
    """Return (and lazily create) the process-level QueuePublisher singleton."""
    global _publisher
    if _publisher is None:
        with _publisher_lock:
            if _publisher is None:
                _publisher = QueuePublisher()
    return _publisher
