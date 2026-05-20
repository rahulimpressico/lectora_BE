"""API process entrypoint."""
from contextlib import asynccontextmanager

from azure.servicebus import ServiceBusClient
from fastapi import FastAPI
from sqlalchemy import text

from lectora_backend.api.middleware.cors import add_cors_middleware
from lectora_backend.api.middleware.logging_middleware import LoggingMiddleware
from lectora_backend.api.routes import events, generate_to, health, jobs, storage
from lectora_backend.config import settings
from lectora_backend.dependencies import engine
from lectora_backend.repositories.blob_repository import BlobRepository


_REQUIRED_SETTINGS = (
    "service_bus_connection_string",
    "azure_openai_api_key",
    "azure_openai_endpoint",
    "azure_storage_connection_string",
)


def _validate_required_settings() -> None:
    missing = [name for name in _REQUIRED_SETTINGS if not getattr(settings, name, "")]
    if missing:
        raise RuntimeError(
            f"Missing required configuration: {', '.join(missing)}"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_required_settings()

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    BlobRepository()

    servicebus_client = ServiceBusClient.from_connection_string(
        settings.service_bus_connection_string
    )
    with servicebus_client:
        sender = servicebus_client.get_queue_sender(queue_name=settings.queue_name)
        with sender:
            pass

    yield


app = FastAPI(title="Lectora Backend", lifespan=lifespan)
app.add_middleware(LoggingMiddleware)
add_cors_middleware(app)  # outermost — handles OPTIONS before other middleware
app.include_router(health.router, prefix="/health", tags=["health"])
app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
# SSE endpoint lives on the same /jobs prefix so it shares job-level auth
app.include_router(events.router, prefix="/jobs", tags=["events"])
app.include_router(storage.router, prefix="/storage", tags=["storage"])
app.include_router(generate_to.router, prefix="/documents", tags=["documents"])
