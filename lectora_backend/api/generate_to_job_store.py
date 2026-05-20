"""
In-memory job store for async POST /documents/generate-to (dev API).

Keeps long-running A0 work off the HTTP request thread so the FE gets an
immediate response and polls for results instead of holding the connection open.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Limit parallel A0 runs — each loads a full DOCX + multiple LLM calls.
_MAX_CONCURRENT = max(1, int(os.environ.get("A0_API_MAX_CONCURRENT", "2")))
_JOB_TTL_SEC = max(300, int(os.environ.get("A0_API_JOB_TTL_SEC", "3600")))


class GenerateTOJobStatus(str, Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class GenerateTOJob:
    job_id: str
    status: GenerateTOJobStatus
    created_at: float
    blob_path: str
    difficulty: str
    message: str = "Queued"
    result: dict[str, Any] | None = None
    error: str | None = None
    output_dir: str | None = None
    finished_at: float | None = None


class GenerateTOJobStore:
    """Thread-safe in-memory store with a concurrency semaphore."""

    def __init__(self) -> None:
        self._jobs: dict[str, GenerateTOJob] = {}
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(_MAX_CONCURRENT)

    def create(self, *, blob_path: str, difficulty: str) -> GenerateTOJob:
        job_id = uuid.uuid4().hex
        job = GenerateTOJob(
            job_id=job_id,
            status=GenerateTOJobStatus.PROCESSING,
            created_at=time.time(),
            blob_path=blob_path,
            difficulty=difficulty,
            message="A0 started",
        )
        with self._lock:
            self._purge_expired_locked()
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> GenerateTOJob | None:
        with self._lock:
            self._purge_expired_locked()
            return self._jobs.get(job_id)

    def update_message(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status == GenerateTOJobStatus.PROCESSING:
                job.message = message

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = GenerateTOJobStatus.COMPLETED
            job.result = result
            job.finished_at = time.time()
            job.message = "A0 complete"
            if job.output_dir:
                shutil.rmtree(job.output_dir, ignore_errors=True)
                job.output_dir = None

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = GenerateTOJobStatus.FAILED
            job.error = error
            job.finished_at = time.time()
            job.message = "A0 failed"
            if job.output_dir:
                shutil.rmtree(job.output_dir, ignore_errors=True)
                job.output_dir = None

    def set_output_dir(self, job_id: str, output_dir: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.output_dir = output_dir

    def acquire_slot(self) -> bool:
        """Non-blocking; returns False if max concurrent A0 runs are active."""
        return self._semaphore.acquire(blocking=False)

    def release_slot(self) -> None:
        self._semaphore.release()

    def _purge_expired_locked(self) -> None:
        now = time.time()
        expired = [
            jid
            for jid, j in self._jobs.items()
            if j.finished_at and (now - j.finished_at) > _JOB_TTL_SEC
        ]
        for jid in expired:
            j = self._jobs.pop(jid, None)
            if j and j.output_dir:
                shutil.rmtree(j.output_dir, ignore_errors=True)

    def queue_count(self) -> int:
        with self._lock:
            return sum(
                1 for j in self._jobs.values()
                if j.status == GenerateTOJobStatus.PROCESSING
            )


_store = GenerateTOJobStore()


def get_generate_to_job_store() -> GenerateTOJobStore:
    return _store


async def run_a0_job_background(
    job_id: str,
    *,
    blob_path: str,
    difficulty: str,
    output_dir: Path,
    runner: Callable[[], Any],
    build_response: Callable[[Any, str], dict[str, Any]],
    slot_acquired: bool = False,
) -> None:
    """
    Run A0 in a worker thread; update job store on success/failure.
    ``build_response`` converts A0Result → serializable dict for the API.

    When ``slot_acquired`` is True, the HTTP handler already took a semaphore
    slot (returns 503 if full); this task must ``release_slot`` in ``finally``.
    """
    store = get_generate_to_job_store()

    if not slot_acquired:
        if not store.acquire_slot():
            store.fail(
                job_id,
                f"Server busy — max {_MAX_CONCURRENT} A0 job(s) already running. Retry shortly.",
            )
            shutil.rmtree(output_dir, ignore_errors=True)
            return

    store.set_output_dir(job_id, str(output_dir))
    store.update_message(job_id, "Parsing document and calling LLM…")

    try:
        t0 = time.perf_counter()
        result = await asyncio.to_thread(runner)
        t_a0 = time.perf_counter()
        payload = build_response(result, difficulty)
        t_done = time.perf_counter()
        store.complete(job_id, payload)
        logger.info(
            "[generate-to] Job %s completed | A0=%.1fs | response_build=%.1fs | total=%.1fs",
            job_id,
            t_a0 - t0,
            t_done - t_a0,
            t_done - t0,
        )
    except Exception as exc:
        logger.exception("[generate-to] Job %s failed: %s", job_id, exc)
        store.fail(job_id, str(exc))
        shutil.rmtree(output_dir, ignore_errors=True)
    finally:
        store.release_slot()
