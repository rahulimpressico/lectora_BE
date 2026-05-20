"""SQL persistence for job and stage metadata."""
import json
from datetime import datetime, timezone

from sqlalchemy import select
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
        job = self.get_job(job_id)
        if job is None:
            return None

        retry_entry = RetryHistory(
            job_id=job_id,
            attempt=len(job.retry_history) + 1,
            from_stage=from_stage,
            section_id=section_id,
            triggered_by=triggered_by,
            triggered_at=datetime.now(timezone.utc),
            overrides=json.dumps(overrides) if overrides is not None else None,
            outcome=StageStatus.PROCESSING,
        )

        job.status = JobStatus.PROCESSING
        job.updated_at = datetime.now(timezone.utc)

        self.session.add(retry_entry)
        self.session.commit()
        return self.get_job(job_id)

    def update_job_status(self, job_id: str, status: JobStatus) -> Job | None:
        job = self.get_job(job_id)
        if job is None:
            return None

        job.status = status
        job.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return self.get_job(job_id)

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
    ) -> StageProgress | None:
        stmt = select(StageProgress).where(
            StageProgress.job_id == job_id,
            StageProgress.stage_id == stage_id,
        )
        stage = self.session.execute(stmt).scalar_one_or_none()
        if stage is None:
            return None

        stage.status = status

        if started_at is not None:
            stage.started_at = started_at

        if completed_at is not None:
            stage.completed_at = completed_at

        stage.validation_outcome = validation_outcome
        stage.error_detail = error_detail

        self.session.commit()
        return stage

    def mark_job_failed(
        self,
        *,
        job_id: str,
        code: str,
        message: str,
        retryable: bool,
    ) -> Job | None:
        job = self.get_job(job_id)
        if job is None:
            return None

        job.status = JobStatus.FAILED
        job.updated_at = datetime.now(timezone.utc)

        first_stage = next(
            (stage for stage in job.stage_progress if stage.stage_id == PipelineStep.A0),
            None,
        )
        if first_stage is not None:
            first_stage.status = StageStatus.FAILED
            first_stage.error_detail = json.dumps(
                {
                    "code": code,
                    "message": message,
                    "stage": None,
                    "retryable": retryable,
                }
            )

        self.session.commit()
        return self.get_job(job_id)
