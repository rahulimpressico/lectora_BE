"""Dashboard summary endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lectora_backend.api.local_course_job_store import get_local_course_job_store
from lectora_backend.api.schemas.dashboard_schemas import DashboardSummaryResponse
from lectora_backend.dependencies import get_db_session
from lectora_backend.models.db_models import Job
from lectora_backend.models.job_enums import JobStatus

router = APIRouter()

_ACTIVE_JOB_STALE_WINDOW = timedelta(hours=2)


def _db_counts(db: Session) -> dict[str, int]:
    """Return DB-backed job counts, or zeros when the jobs table is unavailable."""
    bind = db.get_bind()
    if bind is None:
        return {"courses_generated": 0, "in_progress": 0, "completed": 0}

    try:
        if "jobs" not in inspect(bind).get_table_names():
            return {"courses_generated": 0, "in_progress": 0, "completed": 0}

        cutoff = datetime.now(timezone.utc) - _ACTIVE_JOB_STALE_WINDOW
        in_progress = db.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]))
            .where(Job.updated_at >= cutoff)
        ) or 0
        completed = db.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.status == JobStatus.COMPLETED)
        ) or 0
    except SQLAlchemyError:
        return {"courses_generated": 0, "in_progress": 0, "completed": 0}

    return {
        "courses_generated": int(completed),
        "in_progress": int(in_progress),
        "completed": int(completed),
    }


@router.get("/summary", response_model=DashboardSummaryResponse)
def get_dashboard_summary(
    db: Session = Depends(get_db_session),
) -> DashboardSummaryResponse:
    db_counts = _db_counts(db)
    local_store = get_local_course_job_store()
    local_counts = local_store.get_status_counts()
    cutoff = datetime.now(timezone.utc) - _ACTIVE_JOB_STALE_WINDOW

    return DashboardSummaryResponse(
        coursesGenerated=db_counts["courses_generated"] + local_counts["completed"],
        inProgress=db_counts["in_progress"] + local_store.get_recent_in_progress_count(cutoff),
        completed=db_counts["completed"] + local_counts["completed"],
    )
