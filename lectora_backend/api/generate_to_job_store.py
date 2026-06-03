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
import tempfile
import threading
import time
import uuid

from lectora_backend.core.job_registry import get_generate_to, unregister_generate_to
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Limit parallel A0 runs — each loads a full DOCX + multiple LLM calls.
_MAX_CONCURRENT = max(1, int(os.environ.get("A0_API_MAX_CONCURRENT", "2")))
_JOB_TTL_SEC = max(300, int(os.environ.get("A0_API_JOB_TTL_SEC", "3600")))


def _cleanup_ephemeral_output_dir(output_dir: str | Path | None) -> None:
    """Delete only temp A0 work dirs, never persistent pipeline folders."""
    if not output_dir:
        return
    target = Path(output_dir).expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if target.parent == temp_root and target.name.startswith("lectora_a0_"):
        shutil.rmtree(target, ignore_errors=True)


class GenerateTOJobStatus(str, Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class GenerateTOLogEntry:
    id: int
    ts: float
    level: str
    message: str
    stage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "level": self.level,
            "message": self.message,
            "stage": self.stage,
        }


@dataclass
class GenerateTOJob:
    job_id: str
    status: GenerateTOJobStatus
    created_at: float
    blob_path: str
    blob_paths: list[str]
    course_folder: str | None
    difficulty: str
    message: str = "Queued"
    result: dict[str, Any] | None = None
    error: str | None = None
    output_dir: str | None = None
    finished_at: float | None = None
    logs: list[GenerateTOLogEntry] = field(default_factory=list)


class GenerateTOJobStore:
    """Thread-safe in-memory store with a concurrency semaphore."""

    def __init__(self) -> None:
        self._jobs: dict[str, GenerateTOJob] = {}
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(_MAX_CONCURRENT)

    def create(
        self,
        *,
        blob_path: str,
        blob_paths: list[str] | None = None,
        course_folder: str | None = None,
        difficulty: str,
    ) -> GenerateTOJob:
        job_id = uuid.uuid4().hex
        paths = list(blob_paths or [blob_path])
        job = GenerateTOJob(
            job_id=job_id,
            status=GenerateTOJobStatus.PROCESSING,
            created_at=time.time(),
            blob_path=blob_path,
            blob_paths=paths,
            course_folder=course_folder,
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

    def append_log(
        self,
        job_id: str,
        *,
        level: str,
        message: str,
        stage: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            next_id = job.logs[-1].id + 1 if job.logs else 1
            job.logs.append(
                GenerateTOLogEntry(
                    id=next_id,
                    ts=time.time(),
                    level=level,
                    message=message,
                    stage=stage,
                )
            )
            if job.status == GenerateTOJobStatus.PROCESSING:
                job.message = message

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.status == GenerateTOJobStatus.CANCELLED:
                return
            job.status = GenerateTOJobStatus.COMPLETED
            job.result = result
            job.finished_at = time.time()
            job.message = "A0 complete"
            if job.output_dir:
                _cleanup_ephemeral_output_dir(job.output_dir)
                job.output_dir = None

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.status == GenerateTOJobStatus.CANCELLED:
                return
            job.status = GenerateTOJobStatus.FAILED
            job.error = error
            job.finished_at = time.time()
            job.message = "A0 failed"
            if job.output_dir:
                _cleanup_ephemeral_output_dir(job.output_dir)
                job.output_dir = None

    def cancel(self, job_id: str, *, reason: str = "Cancelled") -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status in (
                GenerateTOJobStatus.COMPLETED,
                GenerateTOJobStatus.FAILED,
                GenerateTOJobStatus.CANCELLED,
            ):
                return False
            job.status = GenerateTOJobStatus.CANCELLED
            job.error = reason
            job.finished_at = time.time()
            job.message = reason
            if job.output_dir:
                _cleanup_ephemeral_output_dir(job.output_dir)
                job.output_dir = None
            return True

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
                _cleanup_ephemeral_output_dir(j.output_dir)

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
    cancel_event: threading.Event | None = None,
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
            _cleanup_ephemeral_output_dir(output_dir)
            return

    store.set_output_dir(job_id, str(output_dir))
    store.append_log(
        job_id,
        level="info",
        message="Preparing A0 run and loading source documents…",
        stage="A0",
    )

    def _runner_with_cancel() -> Any:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Cancelled")
        handle = get_generate_to(job_id)
        if handle and handle.cancel_event.is_set():
            raise RuntimeError("Cancelled")
        return runner()

    def log(level: str, message: str, stage: str | None = None) -> None:
        store.append_log(job_id, level=level, message=message, stage=stage)

    try:
        t0 = time.perf_counter()
        result = await asyncio.to_thread(_runner_with_cancel)
        t_a0 = time.perf_counter()
        payload = build_response(result, difficulty)
        t_done = time.perf_counter()
        log("success", "A0 run complete — generated Training Outline is ready.", "A0")
        store.complete(job_id, payload)
        logger.info(
            "[generate-to] Job %s completed | A0=%.1fs | response_build=%.1fs | total=%.1fs",
            job_id,
            t_a0 - t0,
            t_done - t_a0,
            t_done - t0,
        )
    except Exception as exc:
        msg = str(exc)
        if msg == "Cancelled" or (cancel_event and cancel_event.is_set()):
            log("warn", "A0 run cancelled.", "A0")
            store.cancel(job_id, reason="Cancelled — source files removed or job stopped")
            logger.info("[generate-to] Job %s cancelled", job_id)
        else:
            log("error", f"A0 run failed: {msg}", "A0")
            logger.exception("[generate-to] Job %s failed: %s", job_id, exc)
            store.fail(job_id, msg)
        _cleanup_ephemeral_output_dir(output_dir)
    finally:
        unregister_generate_to(job_id)
        store.release_slot()
