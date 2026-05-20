"""Publish job messages to Azure Service Bus."""
import asyncio
import json
import logging

from azure.servicebus import ServiceBusClient, ServiceBusMessage

from lectora_backend.config import settings


logger = logging.getLogger(__name__)


class QueuePublisher:
    def __init__(self) -> None:
        self._client = ServiceBusClient.from_connection_string(
            conn_str=settings.service_bus_connection_string
        )
        self._queue_name = settings.queue_name

    def _send_message(self, message_body: str) -> None:
        with self._client:
            sender = self._client.get_queue_sender(queue_name=self._queue_name)
            with sender:
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
