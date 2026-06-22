"""Schemas for the course content and AI-operation endpoints."""
from __future__ import annotations

from pydantic import ConfigDict

from lectora_backend.api.schemas.base import CamelModel as _BaseCamelModel, to_camel


class CamelModel(_BaseCamelModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


# ── Course content (GET /jobs/{jobId}/course) ─────────────────────────────────

class SectionImageSchema(CamelModel):
    id: str
    file_name: str
    blob_path: str
    caption: str | None = None
    alt_text: str | None = None


class CourseSectionSchema(CamelModel):
    id: str
    title: str
    level: int
    content: str
    learning_objectives: list[str]
    word_count: int
    has_knowledge_check: bool
    estimated_duration: str | None = None
    order: int
    parent_id: str | None = None
    children: list["CourseSectionSchema"] = []
    images: list[SectionImageSchema] = []


class CourseContentMeta(CamelModel):
    total_word_count: int
    section_count: int
    chapter_count: int
    estimated_read_time: str


class CourseContentResponse(CamelModel):
    job_id: str
    course_title: str
    course_type: str
    generated_at: str
    meta: CourseContentMeta
    sections: list[CourseSectionSchema]


# ── AI operation (POST /jobs/{jobId}/ai) ─────────────────────────────────────

class AIOperationRequest(CamelModel):
    section_id: str
    operation: str      # regenerate | rewrite | improve_tone | summarize | expand | simplify
    content: str
    context: str | None = None
    user_prompt: str | None = None


class AIOperationResponse(CamelModel):
    section_id: str
    operation: str
    content: str
    processing_time_ms: int


# ── Artifact download (GET /jobs/{jobId}/artifacts/download) ─────────────────

class ArtifactDownloadResponse(CamelModel):
    url: str
    filename: str
    content_type: str
