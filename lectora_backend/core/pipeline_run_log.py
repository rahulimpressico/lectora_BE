"""Persist in-memory pipeline job logs to JSON on disk (and optionally Azure)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lectora_backend.api.local_course_job_store import LocalCourseJob

logger = logging.getLogger(__name__)

RUN_LOG_FILENAME = "pipeline_run_log.json"


def run_log_path(artifact_dir: str | Path) -> Path:
    return Path(artifact_dir) / "logs" / RUN_LOG_FILENAME


def flush_job_logs(job: "LocalCourseJob") -> Path | None:
    """Write all in-memory log entries to ``logs/pipeline_run_log.json``."""
    if not job.artifact_dir:
        return None
    target = run_log_path(job.artifact_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "jobId": job.job_id,
        "courseTitle": job.course_title,
        "status": job.status.value,
        "updatedAt": job.updated_at,
        "entries": [entry.to_dict() for entry in job.logs],
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def sync_run_log_to_azure(job: "LocalCourseJob") -> None:
    """Upload the run log JSON to regedlectoraaistorage (pipeline-artifacts view)."""
    if not job.artifact_dir:
        return
    log_file = run_log_path(job.artifact_dir)
    if not log_file.is_file():
        return
    try:
        from lectora_backend.config import settings
        from lectora_backend.core.blob_layout import build_blob_layout_for_course
        from lectora_backend.repositories.blob_repository import BlobRepository

        if not settings.is_azure_storage_configured():
            return
        layout = build_blob_layout_for_course(job.course_title, job_id=job.job_id)
        blob_path = f"{layout.logs_dir}/{RUN_LOG_FILENAME}"
        repo = BlobRepository(container_name=settings.blob_container_name)
        repo.upload_file(
            local_path=str(log_file),
            blob_path=blob_path,
            content_type="application/json",
        )
    except Exception as exc:
        logger.debug("[pipeline_run_log] Azure log sync skipped: %s", exc)
