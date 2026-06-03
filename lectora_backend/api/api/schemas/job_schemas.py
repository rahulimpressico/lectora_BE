"""API request/response contracts for job resources."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lectora_backend.models.job_enums import (
    JobStatus,
    PipelineStep,
    StageStatus,
    ValidationOutcome,
)


def to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class JobInputReference(CamelModel):
    blob_path: str = Field(..., description="Blob path of an uploaded source file.")


class JobInputs(CamelModel):
    course_brief: JobInputReference | None = None
    timed_outline: JobInputReference | None = None
    study_guide: JobInputReference
    exam_reference: JobInputReference | None = None
    compliance_notes: JobInputReference | None = None


class JobCreateRequest(CamelModel):
    course_title: str = Field(..., description="Requested course title.")
    course_type: str = Field(..., description="Requested course family.")
    inputs: JobInputs
    # Optional user-edited Training Outline JSON (from the three-panel TO editor).
    # When present the pipeline injects it into shared_state so A1 uses the
    # user's version instead of re-generating from the original DOCX.
    to_override: dict[str, Any] | None = None
    # All source blob paths (DOCX + PDF) uploaded during the generate-TO step.
    # When provided, A2 downloads these files and uses topic-wise chunk retrieval
    # to enrich each section's source context across all uploaded documents.
    source_file_paths: list[str] | None = None


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
