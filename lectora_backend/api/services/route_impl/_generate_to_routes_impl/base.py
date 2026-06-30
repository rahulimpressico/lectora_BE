"""
POST /documents/upload       — save uploaded DOCX, PDF, or JSON (TO) as-is.
POST /documents/generate-to  — run TO pipeline (A0 → A1 → S1).
GET  /documents/generate-to/jobs/{jobId} — poll async TO pipeline result.

Designed for local / dev usage.  Production paths go through the worker queue.

PDF ingestion
─────────────
PDF files are stored as-is alongside DOCX uploads.  A0 accepts both formats
natively via its ``pdf_paths`` parameter (handled by ``PDFSourceParser``).
No conversion step is required; the caller receives the original file's blob
path and extension.

JSON Timed Outline upload
─────────────────────────
A pre-built or previously-generated Timed Outline can be uploaded as a
``.json`` file.  A0 detects the ``.json`` extension and uses the fast-path
(``json.load``) instead of invoking the LLM, so the pipeline skips outline
re-generation entirely.  Only DOCX/PDF are accepted as *source* documents
for the generate-to endpoint itself; JSON is accepted only as an upload.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Literal
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from lectora_backend.api.generate_to_job_store import (
    get_generate_to_job_store,
    run_a0_job_background,
)
from lectora_backend.core.job_registry import register_generate_to
from lectora_backend.api.schemas.generate_to_schemas import (
    GenerateLearningObjectivesRequest,
    GenerateLearningObjectivesResponse,
    GenerateTOJobAccepted,
    GenerateTOJobPollResponse,
    GenerateTORequest,
    GenerateTOResponse,
    ReviseTORequest,
    ReviseTOResponse,
    SaveTORequest,
    SaveTOResponse,
    SourceAnalysis,
    SourceAnalysisRequest,
    SourceAnalysisResponse,
    SuggestCourseTypeRequest,
    SuggestCourseTypeResponse,
    SuggestOutlineStructureRequest,
    SuggestOutlineStructureResponse,
    SuggestRequiredTopicsRequest,
    SuggestRequiredTopicsResponse,
    UploadDocumentResponse,
)
from lectora_backend.pipeline.agent.a0_request_synthesizer.main import (
    A0RequestSynthesizer,
)
from lectora_backend.pipeline.agent.a1_outline_interpreter.main import run as a1_run
from lectora_backend.pipeline.agent.s1_validator.main import S1Validator
from lectora_backend.pipeline.models import A0Result
from lectora_backend.models.constants import MAX_A0_A1_S1_CYCLES
from lectora_backend.core.blob_layout import sanitize_segment
from lectora_backend.core.course_storage import (
    course_folder_from_blob_path,
)
from lectora_backend.core.blob_paths import UPLOADED_DOCUMENTS_PREFIX
from lectora_backend.core.storage_cleanup import strip_upload_blob_roots as _strip_upload_blob_roots
from lectora_backend.repositories.blob_repository import BlobRepository
from lectora_backend.pipeline.rule_pack_config.rule_packs import resolve_rule_pack
from lectora_backend.pipeline.agent.a0_request_synthesizer.utils.outline_metrics import (
    compute_course_totals,
    get_difficulty_factor,
)
from lectora_backend.api.services.to_response_builder import (
    build_fe_to_response_from_llm_outline,
    clean_rule_pack as _clean_rule_pack,
    clean_sections as _clean_sections,
    find_rule_family_key as _find_rule_family_key,
    normalise_llm_outline as _normalise_llm_outline,
    pick_sections as _pick_sections,
    safe_int as _safe_int,
    unwrap_llm_outline as _unwrap_llm_outline,
)
from lectora_backend.core.pipeline_paths import PIPELINE_SHARED_STATE_DIR
from lectora_backend.pipeline.shared_utils.validation_helpers import s1_blocks
logger = logging.getLogger(__name__)
router = APIRouter()
_UPLOAD_ROOT = Path(tempfile.gettempdir()) / "lectora_uploads"
_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
_LOCAL_SHARED_STATE_DIR = PIPELINE_SHARED_STATE_DIR
_LOCAL_SHARED_STATE_DIR.mkdir(parents=True, exist_ok=True)
_A0_SYNC_TIMEOUT_SEC = max(
    60,
    int(os.environ.get("A0_API_SYNC_TIMEOUT_SEC", "900")),
)
_ALLOWED_EXTENSIONS = {".docx", ".pdf"}
_UPLOAD_ALLOWED_EXTENSIONS = {".docx", ".pdf", ".json"}
_CONTENT_TYPES: dict[str, str] = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".json": "application/json",
}
_ROLE_IMPORTANCE: dict[str, str] = {
    "primary_source": "high",
    "supporting_source": "medium",
    "reference_only": "low",
}
_REVISE_TO_SYSTEM_PROMPT = """\
You are an expert instructional designer. Your task is to revise an existing \
Training Outline (TO) JSON based on the user's instructions.

═══════════════════════════════════════════════════════════
REVISION RULES (CRITICAL — follow all of them)
═══════════════════════════════════════════════════════════
RULE 1 — Output format: Return ONLY the revised Training Outline as a single \
valid JSON object. Do NOT wrap it in markdown code fences, do NOT add any \
explanatory text before or after the JSON.

RULE 2 — Preserve structure: Keep the EXACT same top-level field names and \
nested hierarchy as the input unless the user explicitly asks to add or \
remove sections.

RULE 3 — Preserve metadata: Do NOT change course-level metadata \
(course_name, rule_family, learning_objectives, totals, word counts, \
credit_hours, minutes) unless the user's instruction explicitly requires it.

RULE 4 — Minimal changes: Apply ONLY what the user has requested. Do not \
make unrelated modifications, reorder sections, or rename fields that were \
not mentioned.

RULE 5 — Consistent formatting: Maintain the same numbering style, \
capitalisation, and field schema used in existing sections when adding or \
editing content.

RULE 6 — Return the complete TO: Always return the full Training Outline, \
not just the changed parts.
"""
