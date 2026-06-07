"""
GET /dashboard/summary

Returns live job counts.

Strategy (two sources, one endpoint):
  1. SQL DB (production / Azure): query the `jobs` table when DATABASE_URL
     is configured and reachable.  Counts come from the real Azure job table.
  2. In-memory store (dev mode): fallback when the DB is unavailable or not
     configured (e.g. no absolute SQLite path set).

Dashboard mapping:
  coursesGenerated = total rows in jobs table (all statuses)
  inProgress       = rows with status IN (PENDING, PROCESSING)
                     — these are in the Service Bus queue or actively running
  completed        = rows with status = COMPLETED
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

router = APIRouter()


class DashboardSummary(BaseModel):
    coursesGenerated: int
    inProgress: int
    completed: int


def _counts_from_db() -> DashboardSummary | None:
    """
    Query the SQL jobs table for status counts.
    Returns None if the DB is not configured or unreachable.
    """
    try:
        from lectora_backend.config import settings
        from lectora_backend.dependencies import SessionLocal
        from lectora_backend.models.db_models import Job
        from lectora_backend.models.job_enums import JobStatus

        db = SessionLocal()
        try:
            # Single aggregated query: GROUP BY status
            rows = db.execute(
                select(Job.status, func.count(Job.job_id).label("cnt"))
                .group_by(Job.status)
            ).all()
        finally:
            db.close()

        counts: dict[str, int] = {str(r.status): r.cnt for r in rows}

        total = sum(counts.values())
        in_progress = (
            counts.get(JobStatus.PENDING.value, 0)
            + counts.get(JobStatus.PROCESSING.value, 0)
        )
        completed = counts.get(JobStatus.COMPLETED.value, 0)

        logger.debug(
            "[dashboard] DB counts — total=%d in_progress=%d completed=%d",
            total, in_progress, completed,
        )
        return DashboardSummary(
            coursesGenerated=total,
            inProgress=in_progress,
            completed=completed,
        )

    except (OperationalError, Exception) as exc:
        logger.debug("[dashboard] DB unavailable (%s), falling back to local store", exc)
        return None


def _counts_from_local_store() -> DashboardSummary:
    """Read counts from the in-memory LocalCourseJobStore (dev mode)."""
    from lectora_backend.api.local_course_job_store import (
        LocalJobStatus,
        get_local_course_job_store,
    )

    store = get_local_course_job_store()
    with store._lock:
        jobs = list(store._jobs.values())

    total = len(jobs)
    in_progress = sum(
        1 for j in jobs
        if j.status in (LocalJobStatus.PENDING, LocalJobStatus.PROCESSING)
    )
    completed = sum(1 for j in jobs if j.status == LocalJobStatus.COMPLETED)

    return DashboardSummary(
        coursesGenerated=total,
        inProgress=in_progress,
        completed=completed,
    )


@router.get("/summary", response_model=DashboardSummary)
async def get_dashboard_summary() -> DashboardSummary:
    """
    Live job counts for the dashboard.

    Tries the SQL database first (production / Azure). Falls back to the
    in-memory job store when the DB is not configured (local dev mode).
    """
    result = _counts_from_db()
    if result is not None:
        return result

    return _counts_from_local_store()
