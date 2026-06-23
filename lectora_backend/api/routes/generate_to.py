"""
POST /documents/upload       — save uploaded DOCX, PDF, or JSON (TO) as-is.
POST /documents/generate-to  — run A0 (async by default; optional sync wait).
GET  /documents/generate-to/jobs/{jobId} — poll async A0 result.

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
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

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
    SuggestOutlineStructureRequest,
    SuggestOutlineStructureResponse,
    UploadDocumentResponse,
)
from lectora_backend.pipeline.agent.a0_request_synthesizer.main import (
    A0RequestSynthesizer,
)
from lectora_backend.pipeline.models import A0Result
from lectora_backend.core.blob_layout import sanitize_segment
from lectora_backend.core.course_storage import (
    course_folder_from_blob_path,
)
from lectora_backend.core.blob_paths import UPLOADED_DOCUMENTS_PREFIX
from lectora_backend.core.storage_cleanup import strip_upload_blob_roots as _strip_upload_blob_roots
from lectora_backend.repositories.blob_repository import BlobRepository
from lectora_backend.pipeline.rule_pack_config.rule_packs import (
    RULE_PACKS,
    resolve_rule_pack,
)
from lectora_backend.pipeline.agent.a0_request_synthesizer.utils.outline_metrics import (
    compute_course_totals,
    get_difficulty_factor,
)

logger = logging.getLogger(__name__)


# ── Ingestion background task helpers ────────────────────────────────────────

def _run_ingestion_background(
    file_bytes: bytes,
    filename: str,
    document_id: str,
) -> None:
    """Background task: write bytes to a temp file, run ingestion, clean up."""
    import asyncio
    import tempfile
    import os
    from pathlib import Path
    from lectora_backend.api.ingestion_status_store import set_status

    set_status(document_id, "processing")

    suffix = Path(filename).suffix.lower()
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, prefix=f"ingest_{document_id}_"
        ) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        from lectora_backend.ingestion.service import IngestionOrchestrator
        orchestrator = IngestionOrchestrator.get_instance()

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        result = loop.run_until_complete(
            orchestrator.ingest(tmp_path, document_id, filename)
        )
        logger.info(
            "[ingestion-bg] Completed: document_id=%s chunks=%d status=%s",
            document_id,
            result.total_chunks,
            result.status,
        )
        set_status(document_id, result.status, total_chunks=result.total_chunks)  # type: ignore[arg-type]
    except Exception as exc:
        logger.warning(
            "[ingestion-bg] Failed for document_id=%s filename=%s: %s",
            document_id,
            filename,
            exc,
        )
        set_status(document_id, "failed", error=str(exc))
    finally:
        if tmp_path:
            try:
                import os
                os.unlink(tmp_path)
            except OSError:
                pass
router = APIRouter()

# ── Section-key constants shared by all outline-parsing helpers ───────────────
# Defines the order in which keys are tried when locating the sections list in
# an LLM-generated Training Outline JSON.  Listed most-specific first.
_SECTION_KEYS: tuple[str, ...] = (
    "sections", "lessons", "modules", "table_of_contents", "recommended_scope",
)
# Wrapper keys tried when sections are not found at the top level.
_WRAPPER_KEYS: tuple[str, ...] = (
    "outline", "course_outline", "timed_outline", "to", "result",
)

# ── Local upload storage (dev only) ──────────────────────────────────────────
_UPLOAD_ROOT = Path(tempfile.gettempdir()) / "lectora_uploads"
_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
_LOCAL_SHARED_STATE_DIR = Path(__file__).resolve().parents[2] / "pipeline" / "shared_state"
_LOCAL_SHARED_STATE_DIR.mkdir(parents=True, exist_ok=True)

# Sync wait cap (seconds) — FE can use async mode instead of raising this.
_A0_SYNC_TIMEOUT_SEC = max(
    60,
    int(os.environ.get("A0_API_SYNC_TIMEOUT_SEC", "900")),
)

# Rule-pack keys to strip before sending to the UI (large / internal-only).
_STRIP_FROM_RULES = {
    "difficulty_levels",
    "sample_courses_available",
    "exam_file_format_samples",
    "unique_artifacts",
    "new_course_requested",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_rule_family_key(family_display_name: str) -> str:
    """
    Map a rule-pack ``family`` display name (e.g. "Insurance CE") back to its
    RULE_PACKS dict key (e.g. "insurance_ce").
    Falls back to the first key if nothing matches.
    """
    for key, pack in RULE_PACKS.items():
        if pack.get("family") == family_display_name:
            return key
    return family_display_name.lower().replace(" ", "_")


def _safe_int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _safe_float(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _clean_sections(sections: list[dict]) -> list[dict]:
    """Normalise the llm_to_outline sections list for the UI TO panel."""
    cleaned = []
    for i, s in enumerate(sections):
        subtopics = s.get("subtopics") or []
        subtopic_titles = [
            t.get("title") or t.get("name") or str(t) if isinstance(t, dict) else str(t)
            for t in subtopics
        ]
        cleaned.append({
            "lesson_number": i + 1,
            "title": s.get("title") or s.get("lesson_title") or f"Section {i + 1}",
            "content_summary": (s.get("content") or "")[:300] or None,
            "subtopics": subtopic_titles,
            "word_count": _safe_int(s.get("word_count")),
            "duration_minutes": _safe_int(s.get("minutes")),
            "credit_hours": _safe_float(s.get("credit_hour") or s.get("credit_hours")),
            "interactive_elements": s.get("interactive_elements") or [],
        })
    return cleaned


def _scale_section_word_counts(sections: list[dict], source_word_count: int) -> list[dict]:
    """Proportionally scale section word_count fields so they sum to source_word_count.

    This keeps the per-section distribution from the LLM while anchoring the
    total to the actual source document size (which drives CE credit hours).
    """
    if not sections or source_word_count <= 0:
        return sections
    current_total = sum(_safe_int(s.get("word_count")) for s in sections)
    if current_total <= 0:
        return sections
    ratio = source_word_count / current_total
    scaled = []
    for s in sections:
        scaled.append({**s, "word_count": max(1, round(_safe_int(s.get("word_count")) * ratio))})
    return scaled


def _clean_rule_pack(pack: dict | None) -> dict:
    """Strip internal-only keys from the resolved rule pack."""
    if not pack:
        return {}
    return {k: v for k, v in pack.items() if k not in _STRIP_FROM_RULES}


def _unwrap_llm_outline(raw: dict) -> dict:
    """Normalise saved JSON payloads to the inner llm_to_outline dict."""
    if not raw:
        return {}
    inner = raw.get("llm_to_outline")
    return inner if isinstance(inner, dict) else raw


def _normalise_llm_outline(outline: dict) -> dict:
    """Apply title-alias normalisation to a resolved outline dict.

    Some models return *generated_course_title* instead of *course_title*.
    This helper ensures downstream code always sees *course_title*.
    """
    if not outline.get("course_title") and outline.get("generated_course_title"):
        outline = dict(outline)
        outline["course_title"] = outline["generated_course_title"]
    return outline


def _pick_sections(outline: dict) -> tuple[list[dict], dict]:
    """Try every known section key; also try one level of wrapper keys.

    Returns *(sections_list, resolved_outline)* where *resolved_outline* is
    the dict that actually contained the sections (may be a nested value).
    Uses module-level ``_SECTION_KEYS`` and ``_WRAPPER_KEYS`` constants so
    the key lists are defined exactly once.
    """
    # Fast path: sections at top level
    for key in _SECTION_KEYS:
        sections = outline.get(key)
        if sections and isinstance(sections, list):
            return sections, outline

    # Slow path: sections nested under a wrapper key
    for wk in _WRAPPER_KEYS:
        inner = outline.get(wk)
        if isinstance(inner, dict):
            for key in _SECTION_KEYS:
                sections = inner.get(key)
                if sections and isinstance(sections, list):
                    logger.debug(
                        "[generate-to] Sections found under wrapper key '%s'.'%s'", wk, key
                    )
                    return sections, inner

    return [], outline


def _extract_outline_sections(llm_outline: dict) -> tuple[list[dict], dict, dict]:
    """Return *(sections, totals, resolved_outline)* from heterogeneous TO JSON.

    Delegates section-key discovery to :func:`_pick_sections` so the key list
    is maintained in one place.
    """
    outline = _normalise_llm_outline(_unwrap_llm_outline(llm_outline))
    sections, resolved = _pick_sections(outline)
    totals: dict = resolved.get("totals") or {}
    return sections, totals, resolved


def build_fe_to_response_from_llm_outline(
    llm_outline: dict,
    *,
    difficulty: str = "intermediate",
    shared_state_path: str | None = None,
    rule_family_key: str | None = None,
) -> GenerateTOResponse:
    """Build the FE TO panel payload from a saved llm_to_outline dict."""
    sections, totals, outline = _extract_outline_sections(llm_outline)

    family_key = rule_family_key or "insurance_ce"
    if shared_state_path and Path(shared_state_path).is_file():
        try:
            with open(shared_state_path, encoding="utf-8") as fh:
                state = json.load(fh)
            request_spec = state.get("request_spec") or {}
            rule_cls = request_spec.get("rule_classification") or {}
            family_display = rule_cls.get("family") or ""
            if family_display:
                family_key = _find_rule_family_key(family_display)
            difficulty = (state.get("course_difficulty") or difficulty).strip().lower()
        except Exception:
            pass

    resolved_pack = resolve_rule_pack(family_key, difficulty)
    rules = _clean_rule_pack(resolved_pack)

    cleaned_sections = _clean_sections(sections)
    course_totals = compute_course_totals(cleaned_sections, difficulty=difficulty)

    to: dict[str, Any] = {
        "course_name": outline.get("course_title") or "Untitled Course",
        "rule_family": family_key,
        "difficulty": difficulty,
        "difficulty_factor": get_difficulty_factor(difficulty),
        "audience": outline.get("audience") or "",
        "course_type": outline.get("course_type") or "",
        "topic": outline.get("topic") or "",
        "category": outline.get("category") or "",
        "description": outline.get("description") or "",
        "total_word_count": course_totals["total_word_count"],
        "total_minutes": course_totals["total_minutes"],
        "total_credit_hours": course_totals["total_credit_hours"],
        "learning_objectives": outline.get("learning_objectives") or [],
        "sections": cleaned_sections,
    }
    return GenerateTOResponse(to=to, rules=rules, to_blob_path=None)


def _build_generate_to_response(
    result: A0Result,
    difficulty: str,
    source_blob_path: str | None = None,
) -> GenerateTOResponse:
    """
    Construct the GenerateTOResponse from an A0Result.

    Uses in-memory ``result.llm_to_outline`` when present (fast path after A0);
    otherwise reads ``llm_to_outline.json`` from disk.

    The generated TO is persisted to _UPLOAD_ROOT as a JSON "blob" so the FE can
    pass its path as ``inputs.timedOutline.blobPath`` when creating the main job.
    The pipeline's A0 will then load it directly instead of re-generating it.
    """
    spec = result.request_spec

    rule_family_key = _find_rule_family_key(spec.rule_classification.family)
    resolved_pack = resolve_rule_pack(rule_family_key, difficulty)
    rules = _clean_rule_pack(resolved_pack)

    llm_outline: dict = result.llm_to_outline or {}
    if not llm_outline:
        outline_payload: dict = {}
        try:
            with open(result.output_files.llm_to_outline, encoding="utf-8") as fh:
                outline_payload = json.load(fh)
            llm_outline = outline_payload.get("llm_to_outline") or {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[generate-to] Could not read llm_to_outline: %s", exc)

    # Normalise then extract sections using the shared helpers so the key list
    # is defined exactly once (_SECTION_KEYS / _WRAPPER_KEYS constants).
    llm_outline = _normalise_llm_outline(llm_outline)
    sections, llm_outline = _pick_sections(llm_outline)
    totals: dict = llm_outline.get("totals") or {}

    if not sections:
        top_keys = list(llm_outline.keys()) if llm_outline else []
        logger.error(
            "[generate-to] No sections found in llm_outline after all unwrap attempts. "
            "Top-level keys: %s",
            top_keys,
        )
        raise ValueError(
            f"TO generation produced no sections (tried all known wrapper keys). "
            f"LLM outline top-level keys: {top_keys}. "
            "The source document may lack recognizable structure, or the model "
            "returned an unexpected response format."
        )

    total_doc_word_count = _safe_int(
        getattr(spec, "total_doc_word_count", None) or totals.get("source_word_count")
    )
    if total_doc_word_count > 0 and sections:
        sections = _scale_section_word_counts(sections, total_doc_word_count)

    # Compute accurate totals using the difficulty-adjusted NAIC formula.
    # 180 words = 1 min | 50 min = 1 CE hour | × difficulty factor
    cleaned_sections = _clean_sections(sections)
    course_totals    = compute_course_totals(cleaned_sections, difficulty=difficulty)

    to: dict[str, Any] = {
        "course_name": (
            llm_outline.get("course_title")
            or spec.course_metadata.title
            or "Untitled Course"
        ),
        "rule_family":        rule_family_key,
        "difficulty":         difficulty,
        "difficulty_factor":  get_difficulty_factor(difficulty),
        "audience":           spec.course_metadata.audience or "",
        "course_type":        spec.course_metadata.course_type or "",
        "topic":              spec.course_metadata.topic or "",
        "category":           spec.course_metadata.category or "",
        "description":        llm_outline.get("description") or "",
        "total_word_count":   course_totals["total_word_count"],
        "total_minutes":      course_totals["total_minutes"],
        "total_credit_hours": course_totals["total_credit_hours"],
        "learning_objectives": llm_outline.get("learning_objectives") or [],
        "sections":            cleaned_sections,
        "llm_confidence":      spec.rule_classification.llm_confidence,
        "llm_reasoning":       spec.rule_classification.llm_reasoning,
    }

    # ── Save generated TO as a reusable blob ─────────────────────────────────
    # Downstream: FE passes this path as timedOutline.blobPath in POST /jobs so
    # the main pipeline A0 loads it directly (no re-generation needed).
    to_blob_path: str | None = None
    try:
        folder = _upload_folder_from_blob_path(source_blob_path or "") or uuid.uuid4().hex
        slot = _UPLOAD_ROOT / folder
        slot.mkdir(parents=True, exist_ok=True)
        to_file = slot / "generated_to.json"
        to_file.write_text(
            json.dumps({"llm_to_outline": llm_outline}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        to_blob_path = _uploads_blob_path(folder, "generated_to.json")
        if _azure_storage_ready():
            _uploads_blob_repo().upload_bytes(
                to_blob_path,
                to_file.read_bytes(),
                content_type="application/json",
            )
        logger.info("[generate-to] Saved generated TO blob → %s", to_blob_path)
    except Exception as exc:
        logger.warning("[generate-to] Could not persist TO blob: %s", exc)

    return GenerateTOResponse(to=to, rules=rules, to_blob_path=to_blob_path)


def _result_to_payload(
    result: A0Result,
    difficulty: str,
    source_blob_path: str | None = None,
) -> dict[str, Any]:
    return _build_generate_to_response(
        result, difficulty, source_blob_path=source_blob_path
    ).model_dump(by_alias=True)


def _parse_course_topic(course_topic: str) -> str:
    """Validate and sanitize the user-facing course topic → uploaded-documents/{folder}/."""
    raw = (course_topic or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Course topic is required.",
        )
    if not re.search(r"[A-Za-z0-9]", raw):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Course topic must include at least one letter or number.",
        )
    folder = sanitize_segment(raw)
    if len(folder) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Course topic is too short after normalization.",
        )
    return folder


def _uploads_container_name() -> str:
    from lectora_backend.config import settings  # type: ignore[attr-defined]

    return (
        getattr(settings, "uploaded_documents_container_name", None)
        or UPLOADED_DOCUMENTS_PREFIX
    )


def _uploads_blob_repo() -> BlobRepository:
    return BlobRepository(container_name=_uploads_container_name())


def _uploads_blob_path(folder: str, filename: str) -> str:
    return f"{folder}/{filename}"


def _upload_folder_from_blob_path(blob_path: str) -> str | None:
    clean = _strip_upload_blob_roots(blob_path)
    parts = [p for p in clean.split("/") if p]
    if len(parts) >= 2:
        return parts[0]
    return None


def _azure_storage_ready() -> bool:
    from lectora_backend.config import settings
    return settings.is_azure_storage_configured()


def _validate_document_path(blob_path: str) -> Path:
    """Resolve a blob path (DOCX, PDF, or JSON TO) to a local filesystem Path.

    Accepts DOCX and PDF source documents, plus JSON Timed Outline files.
    Returns a local file path, downloading from Azure Blob Storage when
    available.

    Azure downloads are persisted to ``_UPLOAD_ROOT/{normalized}`` (not a
    disposable temp dir) so that POST /jobs can find the same file by its
    relative blob path after this call completes.

    JSON files are only meaningful as ``to_doc_blob_path`` (pre-built Timed
    Outline).  When a ``.json`` appears in ``blob_paths`` (source docs) it is
    resolved successfully but silently ignored by the ``all_docx``/``all_pdf``
    split downstream — A0 never receives it as a source document.
    """
    from lectora_backend.core.blob_resolver import resolve_blob_to_local

    clean = blob_path.strip().lstrip("/")
    ext = Path(clean).suffix.lower()

    # Relative blob paths: resolved via the shared blob resolver for all
    # supported extensions (DOCX, PDF, and JSON TO files).
    if ext in _UPLOAD_ALLOWED_EXTENSIONS:
        resolved = resolve_blob_to_local(clean)
        if resolved is not None:
            return resolved
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Document not found: '{clean}'. "
                "The file may have been uploaded in a previous session that has since expired. "
                "Please re-upload the document and try again."
            ),
        )

    # Absolute local paths (dev fallback): only DOCX/PDF are accepted here.
    abs_path = Path(blob_path)
    if not abs_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document not found at blobPath: {blob_path}",
        )
    if abs_path.suffix.lower() not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"blobPath must point to a {' or '.join(sorted(_ALLOWED_EXTENSIONS))} file.",
        )
    return abs_path



def _make_a0_runner(
    docx_paths: list[Path],
    pdf_paths: list[Path],
    output_dir: Path,
    difficulty: str,
    extra_text_contents: list[str] | None = None,
    custom_to_prompt: str | None = None,
    course_type_hint: str | None = None,
    to_outline_doc_path: Path | None = None,
    course_output_slug: str | None = None,
    step_logger=None,
    *,
    duration_hours: int | None = None,
    difficulty_level: str | None = None,
    calculated_word_count: int | None = None,
    audience: str | None = None,
    cancel_event: threading.Event | None = None,
):
    """Build a callable that runs A0 on all source DOCX/PDF files with equal priority."""
    def _run_a0() -> A0Result:
        a0 = A0RequestSynthesizer(
            docx_paths=[str(p) for p in docx_paths],
            pdf_paths=[str(p) for p in pdf_paths],
            output_dir=str(output_dir),
            course_difficulty=difficulty,
            extra_text_contents=extra_text_contents or [],
            custom_to_prompt=custom_to_prompt,
            course_type_hint=course_type_hint,
            audience=audience,
            to_outline_doc_path=str(to_outline_doc_path) if to_outline_doc_path else None,
            course_output_slug=course_output_slug,
            step_logger=step_logger,
            duration_hours=duration_hours,
            difficulty_level=difficulty_level,
            calculated_word_count=calculated_word_count,
            cancel_event=cancel_event,
        )
        return a0.run()

    return _run_a0


def _build_explicit_context(body: "GenerateTORequest") -> str | None:
    """Assemble a structured context block from explicit wizard fields.

    This is prepended to ``custom_to_prompt`` so A0 sees well-formatted,
    unambiguous parameters regardless of what the FE composite prompt contains.
    Returns ``None`` when no structured fields are set.
    """
    parts: list[str] = []
    if body.learning_objectives:
        lo_text = "\n".join(f"- {o}" for o in body.learning_objectives)
        parts.append(f"Learning Objectives:\n{lo_text}")
    if body.preferred_chapters is not None:
        parts.append(f"Preferred number of chapters/sections: {body.preferred_chapters}")
    if body.lesson_style:
        style_label = "Short, focused sections" if body.lesson_style == "short" else "Detailed, comprehensive chapters"
        parts.append(f"Lesson style: {style_label}")
    return "\n\n".join(parts) if parts else None


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

_ALLOWED_EXTENSIONS = {".docx", ".pdf"}
# JSON is accepted for the upload endpoint only (pre-built Timed Outline files).
# Source-document validation (_validate_document_path) stays strict to DOCX/PDF.
_UPLOAD_ALLOWED_EXTENSIONS = {".docx", ".pdf", ".json"}
_CONTENT_TYPES: dict[str, str] = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".json": "application/json",
}


@router.post(
    "/upload",
    response_model=UploadDocumentResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
    summary="Upload a DOCX, PDF, or JSON (Timed Outline) file (uploaded-documents container or local temp)",
)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(
        ...,
        description="A .docx or .pdf source document, or a .json Timed Outline file.",
    ),
    course_topic: str = Form(
        ...,
        alias="courseTopic",
        description="Course topic / folder name (required). Creates {topic}/ in uploaded-documents.",
    ),
) -> UploadDocumentResponse:
    """
    Save an uploaded DOCX, PDF, or JSON file under ``{course_topic}/{filename}``
    in the uploaded-documents Azure container (or local dev temp).

    DOCX and PDF files are stored as-is — no conversion is performed.
    A0 handles PDFs natively via ``PDFSourceParser``.

    JSON files must be valid Timed Outline objects (as produced by
    ``POST /documents/generate-to``).  A0 detects the ``.json`` extension and
    uses the fast-path loader, skipping outline re-generation entirely.

    The folder name is derived from the mandatory ``courseTopic`` field (sanitized).
    """
    filename = Path(file.filename or "document.docx").name
    ext = Path(filename).suffix.lower()
    if ext not in _UPLOAD_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only {', '.join(sorted(_UPLOAD_ALLOWED_EXTENSIONS))} files are accepted.",
        )

    folder = _parse_course_topic(course_topic)

    _MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
    try:
        content = await file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read uploaded file — see server logs.",
        ) from exc
    finally:
        await file.close()

    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum allowed upload size is 100 MB.",
        )

    blob_path = _uploads_blob_path(folder, filename)
    document_id = uuid.uuid4().hex[:12]

    from lectora_backend.api.ingestion_status_store import set_status as _set_ingestion_status

    if _azure_storage_ready():
        try:
            _uploads_blob_repo().upload_bytes(
                blob_path,
                content,
                content_type=_CONTENT_TYPES.get(ext, "application/octet-stream"),
            )
        except Exception as exc:
            logger.exception("[upload] Failed to upload to Azure Blob: blob_path=%s", blob_path)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload file to storage — see server logs.",
            ) from exc
        logger.info("[upload] Azure blob %s (%d bytes)", blob_path, len(content))
        # Trigger ingestion for DOCX and PDF files only (skip JSON TO files)
        if ext in {".docx", ".pdf"}:
            _set_ingestion_status(document_id, "pending")
            background_tasks.add_task(
                _run_ingestion_background, content, filename, document_id
            )
        return UploadDocumentResponse(blob_path=blob_path, upload_folder=folder, document_id=document_id)

    slot_dir = _UPLOAD_ROOT / folder
    slot_dir.mkdir(parents=True, exist_ok=True)
    dest = slot_dir / filename
    try:
        dest.write_bytes(content)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save uploaded file: {exc}",
        ) from exc

    logger.info("[upload] Saved %s → %s (%d bytes)", filename, dest, dest.stat().st_size)
    # Trigger ingestion for DOCX and PDF files only (skip JSON TO files)
    if ext in {".docx", ".pdf"}:
        _set_ingestion_status(document_id, "pending")
        background_tasks.add_task(
            _run_ingestion_background, content, filename, document_id
        )
    return UploadDocumentResponse(blob_path=blob_path, upload_folder=folder, document_id=document_id)


@router.get(
    "/{document_id}/ingestion-status",
    summary="Poll background ingestion status for an uploaded document",
)
async def get_ingestion_status(document_id: str) -> JSONResponse:
    """
    Return the ingestion pipeline status for a document uploaded via POST /documents/upload.

    Status values:
      pending    — queued but not yet started
      processing — parse → chunk → enrich → embed → index in progress
      indexed    — fully embedded and indexed in Azure AI Search
      parsed     — chunked but embedding/indexing was skipped (Azure Search not configured)
      failed     — ingestion encountered an unrecoverable error

    Returns 404 when the document_id is unknown or has expired (4 hr TTL).
    """
    from lectora_backend.api.ingestion_status_store import get_status
    entry = get_status(document_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No ingestion record for document_id: {document_id}",
        )
    return JSONResponse(content=entry)


@router.post(
    "/generate-to",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run A0 agent — async by default (poll for result)",
    responses={
        200: {"model": GenerateTOResponse, "description": "Sync mode (wait=true only)"},
        202: {"model": GenerateTOJobAccepted, "description": "Async mode (default)"},
        504: {"description": "Sync mode timed out — use async + poll"},
        503: {"description": "Server busy — too many concurrent A0 jobs"},
    },
)
async def generate_to(
    body: GenerateTORequest,
    wait: bool = Query(
        False,
        description=(
            "If true, block until A0 finishes (may take several minutes; "
            f"times out after {_A0_SYNC_TIMEOUT_SEC}s). Default false = return jobId immediately."
        ),
    ),
):
    """
    Run the **A0 Request Synthesizer** on the document at ``blobPath``.

    **Default (recommended for FE):** returns **202** with ``jobId`` immediately.
    Poll ``GET /documents/generate-to/jobs/{jobId}`` every few seconds until
    ``status`` is ``completed`` or ``failed``.

    **Legacy sync:** ``?wait=true`` holds the connection until A0 finishes.
    """
    blob_paths = body.effective_blob_paths
    difficulty = (body.difficulty or "intermediate").strip().lower()
    custom_to_prompt = (body.custom_to_prompt or "").strip() or None
    # Prepend any explicit wizard fields so A0 sees structured parameters first.
    explicit_ctx = _build_explicit_context(body)
    if explicit_ctx:
        custom_to_prompt = "\n\n".join(filter(None, [explicit_ctx, custom_to_prompt]))
    # Audience flows as a dedicated parameter to A0 → build_dynamic_to_prompt,
    # not as a text prefix injected into the custom prompt.
    audience = (body.audience or "").strip() or None
    course_type_hint = (body.course_type_hint or "").strip() or None

    # ── Dynamic TO flow params (new) ──────────────────────────────────────────
    duration_hours: int | None = body.duration_hours
    difficulty_level: str | None = (body.difficulty_level or "").strip().lower() or None
    calculated_word_count: int | None = body.calculated_word_count

    # Log the exact values received from the frontend before any transformation.
    logger.debug(
        "[generate-to] RAW request payload: difficulty=%r | difficulty_level=%r | "
        "duration_hours=%r | calculated_word_count=%r | audience=%r | "
        "course_type_hint=%r | learning_objectives_count=%d | "
        "preferred_chapters=%r | lesson_style=%r | "
        "blob_paths_count=%d | has_custom_prompt=%s | has_to_doc=%s",
        body.difficulty,
        body.difficulty_level,
        body.duration_hours,
        body.calculated_word_count,
        (body.audience or "")[:80] or None,
        body.course_type_hint,
        len(body.learning_objectives) if body.learning_objectives else 0,
        body.preferred_chapters,
        body.lesson_style,
        len(blob_paths),
        bool(body.custom_to_prompt),
        bool(body.to_doc_blob_path),
    )

    # When the dynamic flow is active, sync the difficulty string so A0 uses it
    # correctly for rule pack + metrics even if the old `difficulty` field
    # wasn't set by the FE.
    if difficulty_level and not body.difficulty:
        difficulty = difficulty_level

    # A0 accepts DOCX and PDF sources natively — separate and pass both.
    resolved_paths = [_validate_document_path(bp) for bp in blob_paths]
    all_docx = [p for p in resolved_paths if p.suffix.lower() == ".docx"]
    all_pdf = [p for p in resolved_paths if p.suffix.lower() == ".pdf"]

    if not all_docx and not all_pdf:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid DOCX or PDF files found in blobPaths.",
        )

    # Resolve optional user-uploaded TO document (DOCX, PDF, or pre-built JSON)
    to_outline_path: Path | None = None
    if body.to_doc_blob_path:
        to_outline_path = _validate_document_path(body.to_doc_blob_path)
        if to_outline_path.suffix.lower() == ".json":
            logger.info(
                "[generate-to] Pre-built JSON TO detected: %s — A0 will use fast-path loader "
                "(no LLM outline generation); rule classification still runs.",
                to_outline_path.name,
            )
        else:
            logger.info("[generate-to] User-provided TO document: %s", to_outline_path.name)

    output_dir = _LOCAL_SHARED_STATE_DIR
    source_blob = blob_paths[0]
    course_folder = course_folder_from_blob_path(source_blob)

    logger.info(
        "[generate-to] Starting A0 | docx=%d | pdf=%d | difficulty=%s | "
        "duration_hours=%s | calculated_word_count=%s | audience=%s | custom_prompt=%s | course_hint=%s | wait=%s",
        len(all_docx),
        len(all_pdf),
        difficulty,
        duration_hours,
        calculated_word_count,
        bool(audience),
        bool(custom_to_prompt),
        bool(course_type_hint),
        wait,
    )

    def _build_runner(step_logger=None, cancel_event: threading.Event | None = None):
        return _make_a0_runner(
            all_docx,
            all_pdf,
            output_dir,
            difficulty,
            custom_to_prompt=custom_to_prompt,
            course_type_hint=course_type_hint,
            to_outline_doc_path=to_outline_path,
            step_logger=step_logger,
            duration_hours=duration_hours,
            difficulty_level=difficulty_level,
            calculated_word_count=calculated_word_count,
            audience=audience,
            cancel_event=cancel_event,
        )

    if wait:
        from lectora_backend.pipeline.shared_llm_config.tracer import set_run_context

        sync_doc_name = all_docx[0].stem if all_docx else (all_pdf[0].stem if all_pdf else "")
        set_run_context(f"sync-{sync_doc_name or 'to'}", sync_doc_name or "unknown")

        runner = _build_runner()
        try:
            result: A0Result = await asyncio.wait_for(
                asyncio.to_thread(runner),
                timeout=_A0_SYNC_TIMEOUT_SEC,
            )
            logger.info(
                "[generate-to] A0 complete (sync) | run_id=%s",
                result.request_spec.run_id,
            )
            response = _build_generate_to_response(
                result, difficulty, source_blob_path=source_blob
            )
            return response
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(
                    f"A0 did not finish within {_A0_SYNC_TIMEOUT_SEC}s. "
                    "Retry without wait=true and poll GET /documents/generate-to/jobs/{{jobId}}."
                ),
            ) from exc
        except ValueError as exc:
            logger.warning("[generate-to] Validation error: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not read document: {exc}",
            ) from exc
        except Exception as exc:
            logger.exception("[generate-to] A0 failed (sync): %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"A0 agent failed: {exc}",
            ) from exc

    # ── Async: return immediately, run A0 in background ─────────────────────
    store = get_generate_to_job_store()
    if not store.acquire_slot():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Server busy — max concurrent A0 jobs reached. "
                f"Active: {store.queue_count()}. Retry in a moment."
            ),
        )

    job = store.create(
        blob_path=source_blob,
        blob_paths=blob_paths,
        course_folder=course_folder,
        difficulty=difficulty,
    )
    poll_url = f"/documents/generate-to/jobs/{job.job_id}"

    reg = register_generate_to(
        job.job_id,
        blob_paths=job.blob_paths,
        course_folder=course_folder,
    )

    def _step_logger(level: str, message: str, stage: str | None = None) -> None:
        store.append_log(job.job_id, level=level, message=message, stage=stage)

    runner = _build_runner(step_logger=_step_logger, cancel_event=reg.cancel_event)

    asyncio.create_task(
        run_a0_job_background(
            job.job_id,
            blob_path=source_blob,
            difficulty=difficulty,
            output_dir=output_dir,
            runner=runner,
            build_response=lambda r, d: _result_to_payload(
                r, d, source_blob_path=source_blob
            ),
            slot_acquired=True,
            cancel_event=reg.cancel_event,
        )
    )

    accepted = GenerateTOJobAccepted(
        job_id=job.job_id,
        status="processing",
        message="A0 started — poll until complete",
        poll_url=poll_url,
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=accepted.model_dump(by_alias=True),
    )


@router.get(
    "/generate-to/jobs",
    summary="List all recent TO-generation jobs (newest first)",
)
async def list_generate_to_jobs() -> JSONResponse:
    store = get_generate_to_job_store()
    jobs = store.list_all()
    return JSONResponse(content=[
        {
            "jobId": j.job_id,
            "status": j.status.value,
            "message": j.message,
            "createdAt": j.created_at,
            "finishedAt": j.finished_at,
            "error": j.error,
            "blobPaths": j.blob_paths,
        }
        for j in jobs
    ])


@router.post(
    "/generate-to/jobs/{job_id}/cancel",
    summary="Cancel an in-flight A0 generate-to job",
)
async def cancel_generate_to_job(job_id: str) -> JSONResponse:
    from lectora_backend.core.job_registry import get_generate_to

    store = get_generate_to_job_store()
    if not store.cancel(job_id, reason="Cancelled by user"):
        job = store.get(job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown or expired jobId: {job_id}",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} is already {job.status.value}",
        )
    handle = get_generate_to(job_id)
    if handle:
        handle.cancel_event.set()
    return JSONResponse(content={"jobId": job_id, "status": "cancelled"})


def _read_json_blob(path: str, source: Literal["uploads", "artifacts"]) -> dict:
    """Load a JSON blob from local temp, pipeline/courses, or Azure."""
    clean = path.strip().lstrip("/")
    if source == "uploads":
        rel = clean
        if rel.startswith(f"{UPLOADED_DOCUMENTS_PREFIX}/"):
            rel = rel[len(UPLOADED_DOCUMENTS_PREFIX) + 1 :]
        local_path = _UPLOAD_ROOT / rel
        if local_path.is_file():
            return json.loads(local_path.read_text(encoding="utf-8"))
        if _azure_storage_ready():
            data = _uploads_blob_repo().download_bytes(rel)
            return json.loads(data.decode("utf-8"))
    else:
        from lectora_backend.config import settings

        if _azure_storage_ready():
            try:
                data = BlobRepository(
                    container_name=settings.course_generation_artifacts_container_name,
                ).download_bytes(clean)
                return json.loads(data.decode("utf-8"))
            except FileNotFoundError:
                pass
            try:
                data = BlobRepository().download_bytes(clean)
                return json.loads(data.decode("utf-8"))
            except FileNotFoundError:
                pass

        from lectora_backend.api.routes.storage import _local_artifact_path_candidates

        courses_dir = Path(__file__).resolve().parents[2] / "pipeline" / "courses"
        legacy_dir = Path(__file__).resolve().parents[2] / "pipeline" / "shared_state"
        for rel in _local_artifact_path_candidates(clean):
            for base in (courses_dir, legacy_dir):
                candidate = base / rel
                if candidate.is_file():
                    return json.loads(candidate.read_text(encoding="utf-8"))
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Training Outline file not found: {path}",
    )


@router.get(
    "/load-to",
    response_model=GenerateTOResponse,
    response_model_by_alias=True,
    summary="Load a saved Training Outline JSON for the TO review panel",
)
async def load_to_from_path(
    path: str = Query(..., description="Blob path from upload or artifact browse"),
    source: Literal["uploads", "artifacts"] = Query(default="uploads"),
) -> GenerateTOResponse:
    payload = _read_json_blob(path, source)
    return build_fe_to_response_from_llm_outline(_unwrap_llm_outline(payload))


@router.post(
    "/generate-learning-objectives",
    response_model=GenerateLearningObjectivesResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
    summary="AI-generate measurable learning objectives from course details",
)
async def generate_learning_objectives(
    body: GenerateLearningObjectivesRequest,
) -> GenerateLearningObjectivesResponse:
    """Use the LLM to produce role-based, outcome-driven learning objectives.

    Accepts any combination of course metadata; the richer the input the more
    targeted the objectives. Source material blob paths are accepted but not
    read inline — the LLM infers from the metadata alone.
    """
    from lectora_backend.pipeline.shared_llm_config.llm import LLMConfig, chat as llm_chat
    from lectora_backend.pipeline.shared_llm_config.model_registry import get_deployment

    config = LLMConfig(
        deployment=get_deployment("A0_TO"),
        max_tokens=2048,
        response_format={"type": "json_object"},
    )

    system_prompt = """\
You are an expert instructional designer specialising in corporate and regulatory \
training courses. Your task is to generate clear, measurable learning objectives \
that follow best-practice instructional design.

═══════════════════════════════════════════════════════════
LEARNING OBJECTIVE RULES (CRITICAL)
═══════════════════════════════════════════════════════════
CONSTRAINT 1 — Count: write exactly 4–6 objectives. Never fewer, never more.
  More than 6 dilutes focus; fewer than 4 fails to cover the course scope.

CONSTRAINT 2 — Bloom's verb required.
  Every objective MUST begin with a measurable action verb from Bloom's Taxonomy:
    Remember:   define, list, recall, identify, name
    Understand: explain, describe, summarize, classify, differentiate
    Apply:      apply, use, demonstrate, calculate, solve
    Analyze:    analyze, compare, distinguish, examine, break down
    Evaluate:   evaluate, justify, recommend, assess, critique
    Create:     design, develop, construct, formulate, propose

  BANNED verbs — these are NOT measurable:
    understand, know, learn, be aware of, appreciate, recognize the importance of,
    gain familiarity with, be introduced to, study

CONSTRAINT 3 — No undefined acronyms.
  Any acronym used in an objective MUST be written out in full on first use.
  Wrong:  "Explain ERISA requirements"
  Right:  "Explain the Employee Retirement Income Security Act (ERISA) requirements
           for employer-sponsored benefit plans"

CONSTRAINT 4 — Learner tasks, not content topics.
  An objective describes what the LEARNER will DO after completing the course —
  not what topics the course COVERS.

  Wrong (content-focused):
    "Understand ERISA"
    "Understand HIPAA"

  Right (learner-focused):
    "Differentiate health plan types — including HMO, PPO, and high-deductible
     plans — and evaluate their suitability for different workforce needs"
    "Apply compliance requirements under major federal benefit laws to common
     employer plan design and administration decisions"

CONSTRAINT 5 — Consolidate regulations into task-based objectives.
  Do NOT write one objective per regulation or acronym.
  Group multiple related regulations under a single job-relevant task:
    "Apply federal compliance obligations — including ERISA, HIPAA, ACA, COBRA,
     and FMLA — to real-world employer plan management scenarios"

VALIDATION STEP — required before finalising each objective:
  1. Does it start with a Bloom's Taxonomy verb?
  2. Does it describe what the learner will DO, not what the course covers?
  3. Are all acronyms spelled out on first use?
  4. Is the total count between 4 and 6?
  If ANY answer is "No" — rewrite the objective.

Return a JSON object with this exact structure:
{"learning_objectives": ["objective 1", "objective 2", ...]}\
"""

    input_parts: list[str] = []
    if body.course_title:
        input_parts.append(f"Course title: {body.course_title}")
    if body.course_description:
        input_parts.append(f"Course description: {body.course_description}")
    if body.course_type:
        input_parts.append(f"Course type: {body.course_type}")
    if body.course_duration:
        input_parts.append(f"Course duration: {body.course_duration}")
    if body.target_audience:
        input_parts.append(f"Target audience: {body.target_audience}")
    if body.skill_level:
        input_parts.append(f"Difficulty level: {body.skill_level}")
    if body.desired_outcomes:
        input_parts.append(f"Desired outcomes: {body.desired_outcomes}")
    if body.certification_focus:
        input_parts.append(f"Certification/compliance focus: {body.certification_focus}")
    if body.additional_instructions:
        input_parts.append(f"Additional instructions: {body.additional_instructions}")

    user_msg = (
        "\n".join(input_parts)
        or "Generate general learning objectives for this training course."
    )

    try:
        raw = await asyncio.to_thread(
            llm_chat, system_prompt, user_msg, config, "LO_GEN"
        )
        data = json.loads(raw)
        objectives: list[str] = data.get("learning_objectives") or []
        if not isinstance(objectives, list):
            objectives = []
        objectives = [str(o).strip() for o in objectives if o]
        logger.info("[generate-learning-objectives] Generated %d objectives", len(objectives))
        return GenerateLearningObjectivesResponse(learning_objectives=objectives)
    except json.JSONDecodeError as exc:
        logger.warning("[generate-learning-objectives] JSON parse error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LLM returned malformed JSON. Please try again.",
        ) from exc
    except Exception as exc:
        logger.exception("[generate-learning-objectives] LLM call failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate objectives: {exc}",
        ) from exc


@router.post(
    "/suggest-outline-structure",
    response_model=SuggestOutlineStructureResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
    summary="AI-suggest chapter count and lesson style for the course outline",
)
async def suggest_outline_structure(
    body: SuggestOutlineStructureRequest,
) -> SuggestOutlineStructureResponse:
    """Analyse course metadata and learning objectives to recommend an outline structure.

    Returns a suggested chapter count, lesson style, and brief reasoning so the
    learner can review before triggering full TO generation.
    """
    from lectora_backend.pipeline.shared_llm_config.llm import LLMConfig, chat as llm_chat
    from lectora_backend.pipeline.shared_llm_config.model_registry import get_deployment

    config = LLMConfig(
        deployment=get_deployment("A0_TO"),
        max_tokens=512,
        response_format={"type": "json_object"},
    )

    system_prompt = (
        "You are an expert instructional designer. Analyse the provided course details "
        "and recommend an optimal outline structure.\n\n"
        "Return a JSON object with this exact structure:\n"
        '{"preferred_chapters": <integer 4-16>, '
        '"lesson_style": "<short|detailed>", '
        '"reasoning": "<one sentence explaining the recommendation>"}'
        "\n\n"
        "Use 'short' when topics are discrete and self-contained; 'detailed' when "
        "each section requires deep explanation or procedural steps."
    )

    input_parts: list[str] = []
    if body.course_title:
        input_parts.append(f"Course title: {body.course_title}")
    if body.course_description:
        input_parts.append(f"Description: {body.course_description}")
    if body.course_type:
        input_parts.append(f"Course type: {body.course_type}")
    if body.target_audience:
        input_parts.append(f"Target audience: {body.target_audience}")
    if body.skill_level:
        input_parts.append(f"Skill level: {body.skill_level}")
    if body.learning_objectives:
        lo_text = "\n".join(f"- {o}" for o in body.learning_objectives[:8])
        input_parts.append(f"Learning objectives:\n{lo_text}")

    user_msg = (
        "\n".join(input_parts)
        or "Recommend a structure for a standard training course."
    )

    try:
        raw = await asyncio.to_thread(
            llm_chat, system_prompt, user_msg, config, "SUGGEST_STRUCTURE"
        )
        data = json.loads(raw)
        preferred_chapters = max(4, min(16, int(data.get("preferred_chapters") or 6)))
        lesson_style = str(data.get("lesson_style") or "short").strip().lower()
        if lesson_style not in ("short", "detailed"):
            lesson_style = "short"
        reasoning = str(data.get("reasoning") or "").strip()
        logger.info(
            "[suggest-outline-structure] chapters=%d style=%s", preferred_chapters, lesson_style
        )
        return SuggestOutlineStructureResponse(
            preferred_chapters=preferred_chapters,
            lesson_style=lesson_style,
            reasoning=reasoning,
        )
    except json.JSONDecodeError as exc:
        logger.warning("[suggest-outline-structure] JSON parse error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LLM returned malformed JSON. Please try again.",
        ) from exc
    except Exception as exc:
        logger.exception("[suggest-outline-structure] LLM call failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to suggest structure: {exc}",
        ) from exc


@router.get(
    "/generate-to/jobs/{job_id}",
    response_model=GenerateTOJobPollResponse,
    response_model_by_alias=True,
    summary="Poll async A0 job status / result",
)
async def get_generate_to_job(job_id: str) -> GenerateTOJobPollResponse:
    """Poll the job started by ``POST /documents/generate-to`` (async mode)."""
    store = get_generate_to_job_store()
    job = store.get(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown or expired jobId: {job_id}",
        )

    if job.status.value == "completed" and job.result:
        return GenerateTOJobPollResponse(
            job_id=job.job_id,
            status="completed",
            message=job.message,
            to=job.result.get("to"),
            rules=job.result.get("rules"),
            to_blob_path=job.result.get("toBlobPath"),
            logs=[log.to_dict() for log in job.logs],
        )

    if job.status.value == "failed":
        return GenerateTOJobPollResponse(
            job_id=job.job_id,
            status="failed",
            message=job.message,
            error=job.error or "A0 failed",
            logs=[log.to_dict() for log in job.logs],
        )

    if job.status.value == "cancelled":
        return GenerateTOJobPollResponse(
            job_id=job.job_id,
            status="cancelled",
            message=job.message,
            error=job.error or "Cancelled",
            logs=[log.to_dict() for log in job.logs],
        )

    return GenerateTOJobPollResponse(
        job_id=job.job_id,
        status="processing",
        message=job.message,
        logs=[log.to_dict() for log in job.logs],
    )
