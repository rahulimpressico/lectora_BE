"""SQL persistence for job and stage metadata."""
import json
from datetime import datetime, timezone

from sqlalchemy import func, select, update as sa_update
from sqlalchemy.orm import Session, selectinload

from lectora_backend.models.db_models import Job, RetryHistory, StageProgress
from lectora_backend.models.job_enums import (
    JobStatus,
    PipelineStep,
    StageStatus,
    ValidationOutcome,
)
from lectora_backend.models.constants import PIPELINE_ORDER


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_job(
        self,
        *,
        job_id: str,
        course_title: str,
        course_type: str,
        requested_by: str,
        shared_state_blob_path: str,
        commit: bool = True,
    ) -> Job:
        now = datetime.now(timezone.utc)
        job = Job(
            job_id=job_id,
            status=JobStatus.PENDING,
            course_title=course_title,
            course_type=course_type,
            requested_by=requested_by,
            shared_state_blob_path=shared_state_blob_path,
            created_at=now,
            updated_at=now,
        )

        job.stage_progress = [
            StageProgress(
                stage_id=stage,
                status=StageStatus.PENDING,
            )
            for stage in PIPELINE_ORDER
        ]

        self.session.add(job)
        self.session.flush()

        if commit:
            self.session.commit()
            self.session.refresh(job)
            return job

        return job

    def get_job(self, job_id: str) -> Job | None:
        stmt = (
            select(Job)
            .where(Job.job_id == job_id)
            .options(
                selectinload(Job.stage_progress),
                selectinload(Job.retry_history),
            )
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def record_retry(
        self,
        *,
        job_id: str,
        from_stage: PipelineStep,
        section_id: str | None,
        overrides: dict[str, object] | None,
        triggered_by: str,
    ) -> Job | None:
        # Lightweight existence check — no need to load relations here.
        exists = self.session.execute(
            select(Job.job_id).where(Job.job_id == job_id)
        ).scalar_one_or_none()
        if exists is None:
            return None

        # Count existing retries in a single aggregate query.
        attempt: int = (
            self.session.execute(
                select(func.count()).select_from(RetryHistory).where(
                    RetryHistory.job_id == job_id
                )
            ).scalar()
            or 0
        ) + 1

        retry_entry = RetryHistory(
            job_id=job_id,
            attempt=attempt,
            from_stage=from_stage,
            section_id=section_id,
            triggered_by=triggered_by,
            triggered_at=datetime.now(timezone.utc),
            overrides=json.dumps(overrides) if overrides is not None else None,
            outcome=StageStatus.PROCESSING,
        )
        self.session.add(retry_entry)

        # Targeted UPDATE — no need to load the full Job + relations.
        self.session.execute(
            sa_update(Job)
            .where(Job.job_id == job_id)
            .values(
                status=JobStatus.PROCESSING,
                updated_at=datetime.now(timezone.utc),
            )
        )
        self.session.commit()

        # One final load so the caller has the full object with updated fields.
        return self.get_job(job_id)

    def update_job_status(self, job_id: str, status: JobStatus) -> None:
        """Update only the job status + updated_at via a targeted SQL UPDATE."""
        self.session.execute(
            sa_update(Job)
            .where(Job.job_id == job_id)
            .values(status=status, updated_at=datetime.now(timezone.utc))
        )
        self.session.commit()

    def update_stage_status(
        self,
        *,
        job_id: str,
        stage_id: PipelineStep,
        status: StageStatus,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        validation_outcome: ValidationOutcome | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Update a single stage row via a targeted SQL UPDATE (no entity reload)."""
        values: dict = {
            "status": status,
            # Always written (including None) so callers can explicitly clear them.
            "validation_outcome": validation_outcome,
            "error_detail": error_detail,
        }
        if started_at is not None:
            values["started_at"] = started_at
        if completed_at is not None:
            values["completed_at"] = completed_at

        self.session.execute(
            sa_update(StageProgress)
            .where(
                StageProgress.job_id == job_id,
                StageProgress.stage_id == stage_id,
            )
            .values(**values)
        )
        self.session.commit()

    def mark_job_failed(
        self,
        *,
        job_id: str,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        """Mark job FAILED and stamp the A0 stage with the error detail."""
        error_json = json.dumps(
            {"code": code, "message": message, "stage": None, "retryable": retryable}
        )

        self.session.execute(
            sa_update(Job)
            .where(Job.job_id == job_id)
            .values(status=JobStatus.FAILED, updated_at=datetime.now(timezone.utc))
        )
        self.session.execute(
            sa_update(StageProgress)
            .where(
                StageProgress.job_id == job_id,
                StageProgress.stage_id == PipelineStep.A0,
            )
            .values(status=StageStatus.FAILED, error_detail=error_json)
        )
        self.session.commit()

    def delete_job(self, job_id: str) -> bool:
        """Remove job and cascaded stage/retry/log rows."""
        job = self.get_job(job_id)
        if job is None:
            return False
        self.session.delete(job)
        self.session.commit()
        return True
