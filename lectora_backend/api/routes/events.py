"""Server-Sent Events endpoint for real-time job pipeline updates.

GET /jobs/{job_id}/events

Streams a `stage_update` SSE event every 2 seconds while a job is active.
Each event includes:
  - Current overall job status
  - Per-stage status, outcome, retry attempt, and inline validation blockers
  - New log entries emitted since the previous event (delta, not full history)

The SSE `id:` field is set to the latest log row ID so the browser can send
`Last-Event-ID` on reconnect, and the server resumes from that cursor.

The connection closes automatically when:
  - The job reaches COMPLETED or FAILED (terminal state)
  - 30 minutes elapse (safety cut-off)

Session lifetime: a **fresh DB session is opened and closed for every poll
tick** so we never hold a connection open for the full 30-minute window.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from lectora_backend.dependencies import SessionLocal
from lectora_backend.repositories.job_repository import JobRepository
from lectora_backend.repositories.job_log_repository import JobLogRepository
from lectora_backend.models.job_enums import JobStatus
from lectora_backend.models.constants import PIPELINE_ORDER, STAGE_ORDER

logger = logging.getLogger(__name__)

router = APIRouter()

_POLL_INTERVAL_SEC = 2.0
_MAX_STREAM_SEC = 30 * 60
_TERMINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.FAILED}


# ── Serialisation helpers ──────────────────────────────────────────────────────

def _serialise_stage(stage) -> dict:
    """Convert a StageProgress ORM row to a JSON-safe dict."""
    result: dict = {
        "stage": stage.stage_id.value,
        "status": stage.status.value,
        "startedAt": stage.started_at.isoformat() if stage.started_at else None,
        "completedAt": stage.completed_at.isoformat() if stage.completed_at else None,
        "outcome": stage.validation_outcome.value if stage.validation_outcome else None,
        "blockers": [],
        "retryAttempt": 0,
    }

    if stage.error_detail:
        try:
            detail = json.loads(stage.error_detail)
            if isinstance(detail, dict):
                result["retryAttempt"] = int(detail.get("gate_cycle", 0))
                raw_blockers = detail.get("blockers") or []
                result["blockers"] = [
                    {
                        "severity": str(b.get("severity", "blocker")),
                        "field": b.get("field"),
                        "message": str(b.get("message", "")),
                    }
                    for b in raw_blockers
                    if isinstance(b, dict)
                ]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return result


def _find_primary_error(ordered_stages) -> dict | None:
    """Return the first failed stage's error detail as a structured dict."""
    for s in ordered_stages:
        if not s.error_detail:
            continue
        try:
            payload = json.loads(s.error_detail)
            if isinstance(payload, dict):
                return {**payload, "stage": s.stage_id.value}
        except (json.JSONDecodeError, TypeError):
            pass
        return {
            "code": "UNKNOWN",
            "message": str(s.error_detail),
            "retryable": False,
            "stage": s.stage_id.value,
        }
    return None


def _serialise_log(log) -> dict:
    return {
        "id": log.id,
        "level": log.level,
        "message": log.message,
        "stageId": log.stage_id,
        "createdAt": (
            log.created_at.isoformat()
            if log.created_at
            else datetime.now(timezone.utc).isoformat()
        ),
    }


def _build_event(job, new_logs: list, latest_log_id: int) -> str:
    ordered = sorted(
        job.stage_progress,
        key=lambda s: STAGE_ORDER.get(s.stage_id, len(PIPELINE_ORDER)),
    )

    error = _find_primary_error(s for s in ordered if s.status.value == "FAILED")

    payload = {
        "type": "stage_update",
        "jobId": job.job_id,
        "status": job.status.value,
        "updatedAt": job.updated_at.isoformat() if job.updated_at else None,
        "stages": [_serialise_stage(s) for s in ordered],
        "error": error,
        "logs": [_serialise_log(lg) for lg in new_logs],
    }
    return f"id: {latest_log_id}\ndata: {json.dumps(payload)}\n\n"


# ── Event generator ────────────────────────────────────────────────────────────

async def _event_generator(job_id: str, last_log_id: int):
    """Yield SSE-formatted strings until the job terminates or timeout.

    Opens a fresh DB session per poll tick and closes it immediately after —
    no connection is held across the asyncio.sleep() call.
    """
    deadline = asyncio.get_event_loop().time() + _MAX_STREAM_SEC
    cursor = last_log_id

    yield ": connected\n\n"

    while asyncio.get_event_loop().time() < deadline:
        # Short-lived session: opened, used, and closed within this block.
        with SessionLocal() as session:
            job = JobRepository(session).get_job(job_id)
            if job is None:
                yield 'event: error\ndata: {"message": "Job not found"}\n\n'
                return

            new_logs = JobLogRepository(session).get_logs_since(
                job_id, after_id=cursor
            )
            latest_log_id = new_logs[-1].id if new_logs else cursor
            event_str = _build_event(job, new_logs, latest_log_id)
            is_terminal = job.status in _TERMINAL_STATUSES

        yield event_str
        cursor = latest_log_id

        if is_terminal:
            yield "event: done\ndata: {}\n\n"
            return

        await asyncio.sleep(_POLL_INTERVAL_SEC)

    yield "event: timeout\ndata: {}\n\n"


# ── Route ──────────────────────────────────────────────────────────────────────

@router.get("/{job_id}/events")
async def stream_job_events(
    job_id: str,
    request: Request,
) -> StreamingResponse:
    """Open an SSE stream that emits pipeline stage-update events for a job.

    Supports reconnect via the `Last-Event-ID` request header (sent automatically
    by the browser's EventSource on reconnect).  Pass `?lastEventId=<n>` as a
    fallback for clients that cannot set custom headers.
    """
    # Verify the job exists before opening the stream — avoids a dangling
    # 200-response SSE connection for a job that doesn't exist.
    with SessionLocal() as session:
        job = JobRepository(session).get_job(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found.",
            )

    raw_cursor = (
        request.headers.get("last-event-id")
        or request.query_params.get("lastEventId")
        or "0"
    )
    try:
        last_log_id = max(0, int(raw_cursor))
    except (ValueError, TypeError):
        last_log_id = 0

    return StreamingResponse(
        _event_generator(job_id, last_log_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
