"""
In-memory job store for async POST /documents/generate-to (dev API).

Keeps long-running TO pipeline work (A0 -> A1 -> S1) off the HTTP request thread so the FE gets an
immediate response and polls for results instead of holding the connection open.

Persistence: completed and failed job results are written to a sidecar JSON file
in ``_CACHE_DIR`` so that polling continues to work across server restarts
(uvicorn --reload re-imports this module and clears the in-memory dict).
"""

from __future__ import annotations

import asyncio
import json
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

# Limit parallel TO runs — each loads full sources + multiple LLM calls.
_MAX_CONCURRENT = max(1, int(os.environ.get("A0_API_MAX_CONCURRENT", "5")))
_JOB_TTL_SEC = max(300, int(os.environ.get("A0_API_JOB_TTL_SEC", "3600")))

# Disk cache for completed/failed job results — survives server restarts.
# Falls back to the system temp directory when the pipeline folder is not writable.
_CACHE_DIR = Path(
    os.environ.get(
        "A0_JOB_CACHE_DIR",
        str(Path(__file__).resolve().parent.parent / "pipeline" / ".generate_to_cache"),
    )
)


def _cache_path(job_id: str) -> Path:
    return _CACHE_DIR / f"{job_id}.json"


def _write_cache(job_id: str, payload: dict[str, Any]) -> None:
    """Write a completed/failed job payload to disk (best-effort)."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(job_id).write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[generate-to cache] Write failed for %s: %s", job_id, exc)


def _read_cache(job_id: str) -> dict[str, Any] | None:
    """Return a previously persisted job payload, or None if not found/expired."""
    p = _cache_path(job_id)
    try:
        if not p.exists():
            return None
        payload = json.loads(p.read_text(encoding="utf-8"))
        finished_at = payload.get("finished_at")
        if finished_at and (time.time() - float(finished_at)) > _JOB_TTL_SEC:
            p.unlink(missing_ok=True)
            return None
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.debug("[generate-to cache] Read failed for %s: %s", job_id, exc)
        return None


def _evict_cache() -> None:
    """Delete expired cache files (best-effort, called on each store.get())."""
    try:
        if not _CACHE_DIR.exists():
            return
        cutoff = time.time() - _JOB_TTL_SEC
        for p in _CACHE_DIR.glob("*.json"):
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
                finished_at = payload.get("finished_at")
                if finished_at and float(finished_at) < cutoff:
                    p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


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
    # Partial S1 validation payload — set when the job fails due to S1 block
    # so the poll endpoint can return validation details alongside the error.
    validation: dict[str, Any] | None = None


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
            job = self._jobs.get(job_id)
            if job:
                return job

        # Not in memory — check disk cache (server may have restarted).
        _evict_cache()
        cached = _read_cache(job_id)
        if not cached:
            return None

        # Reconstruct a minimal read-only job object from the cache file.
        finished_at = cached.get("finished_at")
        cached_status = cached.get("status", "failed")
        stub = GenerateTOJob(
            job_id=job_id,
            status=GenerateTOJobStatus(cached_status),
            created_at=float(finished_at or time.time()),
            blob_path="",
            blob_paths=[],
            course_folder=None,
            difficulty="",
            message=cached.get("message", ""),
            result=cached.get("result"),
            error=cached.get("error"),
            validation=cached.get("validation"),
            finished_at=float(finished_at) if finished_at else None,
            logs=[
                GenerateTOLogEntry(
                    id=lg.get("id", 0),
                    ts=lg.get("ts", 0.0),
                    level=lg.get("level", "info"),
                    message=lg.get("message", ""),
                    stage=lg.get("stage"),
                )
                for lg in (cached.get("logs") or [])
            ],
        )
        with self._lock:
            self._jobs[job_id] = stub
        return stub

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

    def complete(
        self,
        job_id: str,
        result: dict[str, Any],
        *,
        final_message: str = "TO generation complete",
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.status == GenerateTOJobStatus.CANCELLED:
                return
            job.status = GenerateTOJobStatus.COMPLETED
            job.result = result
            job.finished_at = time.time()
            job.message = final_message
            if job.output_dir:
                _cleanup_ephemeral_output_dir(job.output_dir)
                job.output_dir = None
        _write_cache(job_id, {
            "job_id": job_id,
            "status": "completed",
            "message": final_message,
            "result": result,
            "finished_at": job.finished_at,
            "logs": [lg.to_dict() for lg in job.logs],
        })

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        final_message: str = "TO generation failed",
        validation: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.status == GenerateTOJobStatus.CANCELLED:
                return
            job.status = GenerateTOJobStatus.FAILED
            job.error = error
            job.validation = validation
            job.finished_at = time.time()
            job.message = final_message
            if job.output_dir:
                _cleanup_ephemeral_output_dir(job.output_dir)
                job.output_dir = None
        _write_cache(job_id, {
            "job_id": job_id,
            "status": "failed",
            "message": final_message,
            "error": error,
            "validation": validation,
            "finished_at": job.finished_at,
            "logs": [lg.to_dict() for lg in job.logs],
        })

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

    def list_all(self) -> list[GenerateTOJob]:
        """Return all non-expired jobs sorted newest-first."""
        with self._lock:
            self._purge_expired_locked()
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)


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
    Run the TO pipeline in a worker thread; update job store on success/failure.
    ``build_response`` converts the runner output to a serializable dict for the API.

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

    # Attribute A0 / A0_TO traces to the source document so per-doc costing
    # rolls TO generation into the same document as the later course pipeline.
    try:
        from pathlib import Path as _Path
        from lectora_backend.pipeline.shared_llm_config.tracer import set_run_context

        job = store.get(job_id)
        doc_name = ""
        if job and job.blob_paths:
            doc_name = _Path(job.blob_paths[0]).stem
        elif blob_path:
            doc_name = _Path(blob_path).stem
        set_run_context(job_id, doc_name or job_id[:8], source_refs=(job.blob_paths if job and job.blob_paths else ([blob_path] if blob_path else [])))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[generate-to] trace context setup skipped: %s", exc)

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
        log("success", "Pipeline complete — validated Training Outline is ready.", "S1")
        store.complete(job_id, payload, final_message="TO generation complete")
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
            stage = getattr(exc, "stage", "A0")
            log("error", f"{stage} failed: {msg}", stage)
            logger.exception("[generate-to] Job %s failed: %s", job_id, exc)
            # Include S1 validation details when blocked — lets the FE show rich feedback.
            validation = getattr(exc, "validation", None)
            store.fail(job_id, msg, final_message=f"{stage} failed", validation=validation)
        _cleanup_ephemeral_output_dir(output_dir)
    finally:
        unregister_generate_to(job_id)
        store.release_slot()
