"""Job creation, status, retry, artifact, course-content, and AI-operation endpoints."""
import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from lectora_backend.api.schemas.artifact_schemas import ArtifactListResponse, ArtifactSummary
from lectora_backend.api.schemas.course_schemas import (
    AIOperationRequest,
    AIOperationResponse,
    ArtifactDownloadResponse,
    CourseContentMeta,
    CourseContentResponse,
    CourseSectionSchema,
    SectionImageSchema,
)
from lectora_backend.api.schemas.job_schemas import (
    JobCreateRequest,
    JobCreateResponse,
    JobDetailResponse,
    JobErrorDetail,
    RetryRequest,
    RetryResponse,
    StageProgressResponse,
)
from lectora_backend.dependencies import get_db_session
from lectora_backend.api.services.course_content_fallback import get_local_course_content
from lectora_backend.models.job_enums import JobStatus, PipelineStep, StageStatus, ValidationOutcome
from lectora_backend.repositories.job_repository import JobRepository
from lectora_backend.core.blob_layout import build_blob_layout_for_course
from lectora_backend.core.course_storage import sanitize_course_slug
from lectora_backend.core.storage_cleanup import delete_course_output_tree
from lectora_backend.core.state_manager import StateManager
from lectora_backend.core.queue_publisher import get_queue_publisher
from lectora_backend.models.constants import PIPELINE_ORDER, STAGE_ORDER
logger = logging.getLogger(__name__)
router = APIRouter()
_ARTIFACT_STAGE_MAP = {
    "requestSpec": PipelineStep.A0.value,
    "provenanceLog": PipelineStep.A0.value,
    "llmToOutline": PipelineStep.A0.value,
    "courseSpec": PipelineStep.A1.value,
    "a1Status": PipelineStep.A1.value,
    "a1Marker": PipelineStep.A1.value,
    "images": PipelineStep.A0.value,
    "s1Validation": PipelineStep.S1.value,
    "sectionMap": PipelineStep.A2.value,
    "generatedContent": PipelineStep.A2.value,
    "generatedStudyGuide": PipelineStep.A2.value,
    "pipelineSharedState": PipelineStep.A2.value,
}
_AI_OPERATION_PROMPTS: dict[str, str] = {
    "summarize": (
        "You are an expert course content editor. "
        "Summarize the following course section into a concise version that retains all key "
        "learning points, facts, and concepts. Target roughly 40–60% of the original length. "
        "Use clear, direct language. "
        "Output only the summarized content — no preamble, labels, or explanation."
    ),
    "expand": (
        "You are an expert course content writer. "
        "Expand the following course section by adding more depth, concrete examples, "
        "and elaboration on key concepts. Maintain the same educational tone and style. "
        "Only deepen what is already present — do not introduce unrelated topics. "
        "Output only the expanded content — no preamble, labels, or explanation."
    ),
    "simplify": (
        "You are an expert course content editor. "
        "Simplify the following course section by using plainer language, shorter sentences, "
        "and a more accessible writing style. Avoid jargon where possible; "
        "when technical terms are necessary, briefly explain them. "
        "Preserve all factual content and learning points exactly. "
        "Output only the simplified content — no preamble, labels, or explanation."
    ),
    "rewrite": (
        "You are an expert course content writer. "
        "Rewrite the provided course section following the user's instructions exactly. "
        "Preserve all factual content and learning points. "
        "Output only the rewritten section content — no preamble, labels, or explanation."
    ),
    "improve_tone": (
        "You are an expert course content editor specialising in tone and style. "
        "Rewrite the provided course section in the requested tone and style. "
        "Preserve all factual content and learning points. "
        "Output only the revised content — no preamble, labels, or explanation."
    ),
    "regenerate": (
        "You are an expert course content writer. "
        "Fully rewrite the following course section, creating fresh, engaging content "
        "that covers the same topics and learning points with a new perspective. "
        "Output only the rewritten content — no preamble, labels, or explanation."
    ),
}
