"""
Local dev pipeline routes — full course generation without Azure infrastructure.

Exposes the same URL surface that the production main.py provides so the
frontend can target a single /api base URL regardless of which server is running:

    POST   /jobs                        — create & start a pipeline job
    GET    /jobs/{jobId}                — poll job status + stage progress
    GET    /jobs/{jobId}/events         — SSE stream (stage_update events)
    GET    /jobs/{jobId}/course         — course content (completed jobs only)
    POST   /jobs/{jobId}/ai             — AI section operations (stub)
    GET    /jobs/{jobId}/artifacts      — artifact manifest
    GET    /jobs/{jobId}/artifacts/download — docx download

Architecture mirrors generate_to.py:
  - POST /jobs queues a background asyncio task via asyncio.to_thread
  - The sync runner (_run_pipeline_sync) calls pipeline agents directly
  - The in-memory LocalCourseJobStore tracks progress + logs
  - SSE /events streams store state to the frontend every second
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from lectora_backend.core.pipeline_paths import PIPELINE_SHARED_STATE_DIR as _PIPELINE_COURSES_DIR

_PIPELINE_COURSES_DIR.mkdir(parents=True, exist_ok=True)
from lectora_backend.api.local_course_job_store import (
    LocalJobStatus,
    get_local_course_job_store,
)
from lectora_backend.core.course_storage import sanitize_course_slug
from lectora_backend.core.job_registry import register_local_pipeline, unregister_local_pipeline
from lectora_backend.core.storage_cleanup import delete_course_output_tree
from lectora_backend.models.constants import MAX_A2_S2_CYCLES
from lectora_backend.pipeline.agent.a0_request_synthesizer.main import (
    A0RequestSynthesizer,
)
from lectora_backend.pipeline.agent.a1_outline_interpreter.main import run as a1_run
from lectora_backend.pipeline.agent.a2_content_generator.main import (
    A2ContentGenerator,
    render_study_guide_from_state,
)
from lectora_backend.pipeline.agent.a2_content_generator.step_04_render_docx.utils.doc_formatter import (
    _inject_missing_lesson_parent_sections,
)
from lectora_backend.pipeline.agent.kc_planner.main import run as kc_planner_run
from lectora_backend.pipeline.agent.s2_validator.main import S2Validator
from lectora_backend.pipeline.agent.section_mapper.main import run as section_mapper_run
from lectora_backend.pipeline.shared_utils.validation_helpers import (
    format_s2_feedback,
    llm_outline_from_to_data,
    s2_blocks,
)
logger = logging.getLogger(__name__)
router = APIRouter()
_SSE_POLL_INTERVAL = 1.0  # seconds between SSE frames
