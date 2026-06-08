"""
GET /dashboard/summary

All counts come from Azure — no local filesystem or in-memory store.

  coursesGenerated / completed
      Study-guide blobs across Azure containers:
        • generated-courses
        • course-generation-artifacts
        • regedlectoraaistorage (main/legacy)

  inProgress
      PENDING + PROCESSING rows in the SQL ``jobs`` table (production queue/worker).
      Returns 0 when the DB is unavailable.

Why coursesGenerated == completed:
  study_guide.docx is written only after S2 passes. Each matching blob = one
  completed course.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


class DashboardSummary(BaseModel):
    coursesGenerated: int
    inProgress: int
    completed: int
    dataSource: str = "azure_blob"


def _in_progress_from_db() -> int:
    """Count PENDING + PROCESSING rows in the SQL jobs table."""
    try:
        from sqlalchemy import func, select
        from lectora_backend.dependencies import SessionLocal
        from lectora_backend.models.db_models import Job
        from lectora_backend.models.job_enums import JobStatus

        db = SessionLocal()
        try:
            rows = db.execute(
                select(Job.status, func.count(Job.job_id).label("cnt"))
                .group_by(Job.status)
            ).all()
        finally:
            db.close()

        counts: dict[str, int] = {str(r.status): r.cnt for r in rows}
        in_progress = (
            counts.get(JobStatus.PENDING.value, 0)
            + counts.get(JobStatus.PROCESSING.value, 0)
        )
        logger.debug("[dashboard] DB in_progress=%d", in_progress)
        return in_progress

    except Exception as exc:
        logger.debug("[dashboard] DB unavailable for in_progress (%s)", exc)
        return 0


def _is_study_guide_blob(name: str) -> bool:
    """True when blob path is a completed course study guide under /output/."""
    lower = name.lower()
    return "/output/" in lower and "study_guide" in lower and lower.endswith(".docx")


def _count_study_guides_in_container(container_name: str) -> int:
    """Return number of study_guide blobs in a single Azure container."""
    try:
        from lectora_backend.repositories.blob_repository import BlobRepository

        repo = BlobRepository(container_name=container_name)
        blobs = repo.list_blobs("")
        count = sum(1 for b in blobs if _is_study_guide_blob(b))
        logger.debug("[dashboard] container=%r study_guides=%d", container_name, count)
        return count
    except Exception as exc:
        logger.debug(
            "[dashboard] container=%r list failed (%s) — skipping", container_name, exc
        )
        return 0


def _completed_from_azure() -> int:
    """
    Count completed courses across all Azure containers that hold study guide outputs.
    """
    from lectora_backend.config import settings

    if not settings.azure_storage_connection_string.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Azure Blob Storage is not configured (AZURE_STORAGE_CONNECTION_STRING).",
        )

    containers_to_scan = [
        settings.generated_courses_container_name,
        settings.course_generation_artifacts_container_name,
        settings.blob_container_name,
    ]
    seen: set[str] = set()
    unique_containers = [
        c for c in containers_to_scan
        if c and c.strip() and not (c in seen or seen.add(c))  # type: ignore[func-returns-value]
    ]

    total = sum(_count_study_guides_in_container(c) for c in unique_containers)
    logger.debug(
        "[dashboard] Azure completed=%d containers=%s", total, unique_containers
    )
    return total


@router.get("/summary", response_model=DashboardSummary)
async def get_dashboard_summary() -> DashboardSummary:
    """
    Live dashboard counts — Azure Blob only.

    coursesGenerated == completed (study_guide blobs in Azure).
    inProgress = PENDING + PROCESSING from SQL jobs table.
    """
    completed = _completed_from_azure()
    in_progress = _in_progress_from_db()

    return DashboardSummary(
        coursesGenerated=completed,
        inProgress=in_progress,
        completed=completed,
        dataSource="azure_blob",
    )
