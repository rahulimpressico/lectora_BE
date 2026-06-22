"""API request/response contracts for job resources."""
from datetime import datetime
from typing import Any

from pydantic import Field

from lectora_backend.api.schemas.base import CamelModel
from lectora_backend.models.job_enums import (
    JobStatus,
    PipelineStep,
    StageStatus,
    ValidationOutcome,
)


class JobInputReference(CamelModel):
    blob_path: str = Field(..., description="Blob path of an uploaded source file.")


class JobInputs(CamelModel):
    course_brief: JobInputReference | None = None
    timed_outline: JobInputReference | None = None
    study_guide: JobInputReference
    exam_reference: JobInputReference | None = None
    compliance_notes: JobInputReference | None = None


class SourceFileSpec(CamelModel):
    blob_path: str
    extract_hint: str | None = None
    importance: str | None = None  # 'high' | 'medium' | 'low'


class JobCreateRequest(CamelModel):
    course_title: str = Field(..., min_length=1, max_length=200, description="Requested course title.")
    course_type: str = Field(..., min_length=1, max_length=50, description="Requested course family.")
    inputs: JobInputs
    # Optional user-edited Training Outline JSON (from the three-panel TO editor).
    # When present the pipeline injects it into shared_state so A1 uses the
    # user's version instead of re-generating from the original DOCX.
    to_override: dict[str, Any] | None = None
    # Per-file source specs with blob path, extract hint, and importance level.
    # Replaces the flat source_file_paths list — blob paths are derived from these.
    source_file_specs: list[SourceFileSpec] | None = None


class StageProgressResponse(CamelModel):
    stage: PipelineStep
    status: StageStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    outcome: ValidationOutcome | None = None


class JobErrorDetail(CamelModel):
    code: str
    message: str
    stage: PipelineStep | None = None
    retryable: bool


class JobCreateResponse(CamelModel):
    job_id: str
    status: JobStatus


class JobDetailResponse(CamelModel):
    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    stages: list[StageProgressResponse]
    error: JobErrorDetail | None = None


class RetryRequest(CamelModel):
    from_stage: PipelineStep
    section_id: str | None = None
    overrides: dict[str, object] | None = None


class RetryResponse(CamelModel):
    job_id: str
    status: JobStatus
    retry_from_stage: PipelineStep
    section_id: str | None = None
    overrides: dict[str, object] | None = None
