"""
FastAPI application for local development.

Exposes the full course generation pipeline without Azure infrastructure:

    /documents/*    — A0 generate-TO flow (upload + async A0 run)
    /jobs/*         — Full pipeline (A0→A1→S1→Section Mapper→KC Planner→A2→S2)
                      using an in-memory job store and local filesystem

Run:
    uvicorn lectora_backend.dev_app:app --reload --port 8000

UI proxy (vite.config.ts):
    /api  →  http://localhost:8000
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

# Safety guard — dev_app must never run in a production environment
if os.getenv("ENVIRONMENT", "").lower() == "production":
    raise RuntimeError(
        "dev_app.py MUST NOT run in production. "
        "Use main.py for production deployments. "
        "Set ENVIRONMENT != 'production' or use the correct entry point."
    )

from lectora_backend.core.logging_config import configure_logging

configure_logging()

from fastapi import FastAPI

from lectora_backend.api.middleware.cors import add_cors_middleware
from lectora_backend.api.routes import health
from lectora_backend.api.routes import generate_to
from lectora_backend.api.routes import local_jobs
from lectora_backend.api.routes import storage
from lectora_backend.api.routes import settings as settings_routes
from lectora_backend.api.routes import dashboard
from lectora_backend.api.routes import costing
from lectora_backend.pipeline.shared_llm_config import flush_langfuse, shutdown_langfuse

logger = logging.getLogger(__name__)

_REQUIRED = ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT")


def _validate_openai_settings() -> None:
    missing = [k for k in _REQUIRED if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError(
            "Dev app is missing required Azure OpenAI settings: "
            + ", ".join(missing)
            + "\nAdd them to your .env file and restart."
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _validate_openai_settings()
    logger.info(
        "[dev_app] Azure OpenAI credentials OK — dev server ready "
        "(LOG_LEVEL=%s; [EXTRACT]/[TO-LLM]/[A0] logs go to this terminal).",
        os.getenv("LOG_LEVEL", "INFO"),
    )
    yield
    flush_langfuse()
    shutdown_langfuse()


app = FastAPI(
    title="AI Course Generation API (Dev)",
    description=(
        "Local development server — full pipeline via in-memory job store.\n\n"
        "Supports the complete frontend workflow: upload → generate-TO → "
        "three-panel review → pipeline → course editor.\n\n"
        "No Azure Service Bus or Blob Storage required."
    ),
    version="0.1.0-dev",
    lifespan=lifespan,
)

add_cors_middleware(app)

app.include_router(health.router,          prefix="/health",    tags=["health"])
app.include_router(generate_to.router,     prefix="/documents",  tags=["documents"])
app.include_router(local_jobs.router,      prefix="/jobs",       tags=["jobs"])
app.include_router(storage.router,         prefix="/storage",    tags=["storage"])
app.include_router(settings_routes.router, prefix="/settings",   tags=["settings"])
app.include_router(dashboard.router,       prefix="/dashboard",  tags=["dashboard"])
app.include_router(costing.router,         prefix="/costing",    tags=["costing"])


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "app": "AI Course Generation API (Dev)",
        "docs": "/docs",
        "health": "/health/",
        "endpoints": [
            "POST /documents/upload",
            "POST /documents/generate-to  → 202 + jobId (async default)",
            "GET  /documents/generate-to/jobs/{jobId}  → poll A0 result",
            "POST /jobs                   → start full pipeline job",
            "GET  /jobs/{jobId}           → poll job status",
            "GET  /jobs/{jobId}/events    → SSE stage updates",
            "GET  /jobs/{jobId}/course    → course content (completed jobs)",
            "GET  /jobs/{jobId}/artifacts → artifact list",
            "GET  /jobs/{jobId}/artifacts/download → docx download",
            "GET  /storage/browse              → pipeline artifacts",
            "GET  /storage/uploaded-documents/browse → uploaded DOCX",
            "GET  /storage/file                → preview / download",
            "POST /storage/delete              → delete selected files",
            "GET  /dashboard/summary           → live job counts (Courses Generated, In Progress, Completed)",
            "GET  /costing/summary             → aggregated LLM cost + token usage",
            "GET  /costing/documents/{docId}   → per-document cost breakdown",
        ],
    }
