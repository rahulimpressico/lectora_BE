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

try:
    from lectora_backend.core.pipeline_paths import PIPELINE_SHARED_STATE_DIR as _PIPELINE_COURSES_DIR
except ModuleNotFoundError:
    # Backward-compatible fallback for environments that don't yet include
    # the central pipeline_paths module.
    _PIPELINE_COURSES_DIR = Path(__file__).resolve().parents[4] / "pipeline" / "shared_state"

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
try:
    from lectora_backend.pipeline.shared_utils.validation_helpers import (
        format_s2_feedback,
        llm_outline_from_to_data,
        s2_blocks,
    )
except ModuleNotFoundError:
    # Fallback for container images built before validation_helpers.py was added.
    from collections import defaultdict

    def s2_blocks(status: Any) -> bool:  # type: ignore[misc]  # noqa: F841
        _BLOCKING = {"blocked", "blocker"}
        v = status.value if hasattr(status, "value") else str(status)
        return v in _BLOCKING

    def format_s2_feedback(report: Any) -> str:  # type: ignore[misc]  # noqa: F841
        def _get(obj: Any, attr: str, default: str = "") -> Any:
            return obj.get(attr, default) if isinstance(obj, dict) else getattr(obj, attr, default)
        buckets: dict[str, list[str]] = defaultdict(list)
        for issue in _get(report, "issues", []) or []:
            field = _get(issue, "field", "?")
            message = _get(issue, "message", str(issue))
            rule_source = _get(issue, "rule_source", "?")
            severity = _get(issue, "severity", "warning")
            buckets[severity].append(f"  - [{field}] {message} (rule: {rule_source})")
        lines: list[str] = []
        for label, key in [("Blockers (must fix):", "blocker"), ("Critical issues:", "critical"), ("Warnings:", "warning")]:
            if buckets[key]:
                lines.append(label)
                lines.extend(buckets[key])
        return "\n".join(lines)

    def llm_outline_from_to_data(to_data: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]  # noqa: F841
        sections = []
        for s in to_data.get("sections") or []:
            raw_subtopics = s.get("subtopics") or []
            sections.append({
                "title": s.get("title") or "",
                "word_count": s.get("word_count"),
                "minutes": s.get("duration_minutes"),
                "credit_hours": s.get("credit_hours"),
                "content": s.get("content_summary") or "",
                "interactive_elements": s.get("interactive_elements") or [],
                "subtopics": [{"title": t} if isinstance(t, str) else t for t in raw_subtopics],
            })
        return {
            "course_title": to_data.get("course_name") or to_data.get("course_title") or "",
            "description": to_data.get("description") or "",
            "learning_objectives": to_data.get("learning_objectives") or [],
            "totals": {"word_count": to_data.get("total_word_count"), "minutes": to_data.get("total_minutes"), "credit_hours": to_data.get("total_credit_hours")},
            "sections": sections,
            "_user_edited": True,
            "_reused_from_preview": True,
        }
logger = logging.getLogger(__name__)
router = APIRouter()
_SSE_POLL_INTERVAL = 1.0  # seconds between SSE frames
