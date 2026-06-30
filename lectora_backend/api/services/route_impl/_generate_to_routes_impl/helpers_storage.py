from .base import *

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
    duration_hours: float | None = None,
    difficulty_level: str | None = None,
    calculated_word_count: int | None = None,
    audience: str | None = None,
    course_description: str | None = None,
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
            course_description=course_description,
            cancel_event=cancel_event,
        )
        return a0.run()

    return _run_a0

def _build_explicit_context(body: "GenerateTORequest") -> str | None:
    """Assemble a structured context block from explicit wizard fields.

    This is prepended to ``custom_to_prompt`` so A0 sees well-formatted,
    unambiguous parameters regardless of what the FE composite prompt contains.
    Returns ``None`` when no structured fields are set.

    The LOCKED FIELDS block (course_title, description, learning_objectives) is
    placed first and instructs the LLM to copy these user-provided values verbatim.
    A post-processing enforcement step in the route handler guarantees these values
    even if the LLM ignores the instructions.
    """
    parts: list[str] = []

    # ── LOCKED FIELDS — must be copied verbatim into the JSON output ──────────
    locked_lines: list[str] = []
    has_title = bool(body.course_title and body.course_title.strip())
    has_desc  = bool(body.course_description and body.course_description.strip())
    has_los   = bool(body.learning_objectives)

    if has_title:
        locked_lines.append(f'  course_title   → "{body.course_title.strip()}"')  # type: ignore[union-attr]
    if has_desc:
        desc_preview = body.course_description.strip()  # type: ignore[union-attr]
        locked_lines.append(
            f"  description    → Use EXACTLY the text below (do NOT rewrite, shorten, or enhance):\n"
            f'"""\n{desc_preview}\n"""'
        )
    if has_los:
        lo_lines = "\n".join(f"    {i + 1}. {o}" for i, o in enumerate(body.learning_objectives))
        locked_lines.append(
            f"  learning_objectives → Copy these {len(body.learning_objectives)} objectives VERBATIM "
            f"(do NOT add, remove, reword, or reorder them):\n{lo_lines}"
        )

    if locked_lines:
        parts.append(
            "═══════════════════════════════════════════════════════════\n"
            "LOCKED FIELDS — COPY VERBATIM INTO JSON OUTPUT\n"
            "═══════════════════════════════════════════════════════════\n"
            "The course author has provided the following values. They MUST appear\n"
            "character-for-character in the JSON output. Do NOT generate, rewrite,\n"
            "summarize, enhance, or infer alternatives for these fields.\n\n"
            + "\n\n".join(locked_lines)
            + "\n\n"
            "Generate ONLY the section structure (sections, subtopics, word counts,\n"
            "timings, para indices). All other JSON fields are your responsibility."
        )

    # ── Course identity (guidance only — not locked) ──────────────────────────
    # course_description is already in the locked block above; skip here.

    # ── Audience & experience ─────────────────────────────────────────────────
    if body.experience_level and body.experience_level.strip():
        level_labels = {"new": "New to Topic (little or no prior knowledge)",
                        "some": "Some Experience (familiar with core concepts)",
                        "experienced": "Experienced (strong existing knowledge)"}
        label = level_labels.get(body.experience_level.strip().lower(), body.experience_level.strip())
        parts.append(f"Learner Experience Level: {label}")

    if body.learner_outcomes and body.learner_outcomes.strip():
        parts.append(f"Desired Learner Outcomes:\n{body.learner_outcomes.strip()}")

    if body.audience_notes and body.audience_notes.strip():
        parts.append(f"Additional Learner Context:\n{body.audience_notes.strip()}")

    # ── Learning objectives ───────────────────────────────────────────────────
    if body.learning_objectives:
        lo_text = "\n".join(f"- {o}" for o in body.learning_objectives)
        parts.append(f"Learning Objectives:\n{lo_text}")

    # ── Content direction ─────────────────────────────────────────────────────
    if body.tone and body.tone.strip():
        parts.append(f"Writing Tone: {body.tone.strip()}")

    if body.depth and body.depth.strip():
        depth_labels = {"overview": "Overview (high-level introduction, minimal detail)",
                        "balanced": "Balanced (mix of concepts and application)",
                        "detailed": "Detailed (thorough, in-depth coverage)"}
        label = depth_labels.get(body.depth.strip().lower(), body.depth.strip())
        parts.append(f"Course Depth: {label}")

    if body.emphasis and body.emphasis.strip():
        parts.append(f"Topics to Emphasise: {body.emphasis.strip()}")

    if body.avoid and body.avoid.strip():
        parts.append(f"Topics/Approaches to Avoid: {body.avoid.strip()}")

    # ── Instructional design flags ────────────────────────────────────────────
    if body.include_scenarios is not None:
        parts.append(f"Include Real-World Scenarios: {'Yes' if body.include_scenarios else 'No'}")

    if body.include_knowledge_checks is not None:
        parts.append(f"Include Knowledge Checks: {'Yes' if body.include_knowledge_checks else 'No'}")

    # ── Outline structure ─────────────────────────────────────────────────────
    if body.preferred_chapters is not None:
        parts.append(f"Preferred number of chapters/sections: {body.preferred_chapters}")

    if body.lesson_style:
        style_label = "Short, focused sections" if body.lesson_style == "short" else "Detailed, comprehensive chapters"
        parts.append(f"Lesson style: {style_label}")

    # ── Required topics (must appear in every generated TO) ──────────────────
    if body.required_topics:
        rt_lines: list[str] = [
            "REQUIRED TOPICS — MANDATORY COVERAGE",
            "The following topics MUST appear in the generated training outline.",
            "They are non-negotiable and take highest priority over any deprioritisation signals:",
            "",
        ]
        for topic in body.required_topics:
            rt_lines.append(f"  • {topic}")
        rt_lines.append("")
        rt_lines.append("Every required topic above must be represented by at least one dedicated section or subtopic.")
        parts.append("\n".join(rt_lines))

    # ── Source analysis guidance ──────────────────────────────────────────────
    if body.source_analyses:
        sa_lines: list[str] = [
            "SOURCE ANALYSIS GUIDANCE",
            "The following sources have been pre-analyzed. Weight your content selection accordingly:",
            "",
        ]
        for sa in body.source_analyses:
            sa_lines.append(f"[{sa.source_name}]")
            sa_lines.append(f"  Role: {sa.source_role}")
            if sa.extract_hint:
                sa_lines.append(f"  What to get from this source: {sa.extract_hint}")
            if sa.main_topics:
                sa_lines.append(f"  Key topics: {', '.join(sa.main_topics)}")
            if sa.recommended_course_use:
                sa_lines.append(f"  How to use: {sa.recommended_course_use}")
            if sa.recommended_depth:
                sa_lines.append(f"  Coverage depth: {sa.recommended_depth}")
            if sa.supports_learning_objectives:
                sa_lines.append("  Supports LOs:")
                for lo in sa.supports_learning_objectives:
                    sa_lines.append(f"    - {lo}")
            if sa.ignore_or_reduce:
                sa_lines.append("  Deprioritise:")
                for ig in sa.ignore_or_reduce:
                    sa_lines.append(f"    - {ig}")
            sa_lines.append("")
        sa_lines.extend([
            "Weighting rules:",
            "  - Honour each source's 'What to get from this source' guidance above all else",
            "  - primary_source → these topics should dominate the course structure",
            "  - supporting_source → incorporate only into sections where directly relevant",
            "  - reference_only → cite for edge cases; do not build sections around this source",
            "  - Ignore/reduce topics listed above should be minimised or omitted",
        ])
        parts.append("\n".join(sa_lines))

    return "\n\n".join(parts) if parts else None

def _importance_for_source(source_role: str, explicit: str | None = None) -> str:
    """Map source role to coverage weight; honour legacy explicit values when provided."""
    if explicit and explicit in {"high", "medium", "low"}:
        return explicit
    # Legacy FE values from the old importance picker
    legacy_map = {
        "core": "high",
        "supporting": "medium",
        "reference_only": "low",
        "ignore": "low",
    }
    if explicit and explicit in legacy_map:
        return legacy_map[explicit]
    return _ROLE_IMPORTANCE.get(source_role, "medium")

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

        from lectora_backend.core.artifact_paths import local_artifact_path_candidates
        from lectora_backend.core.pipeline_paths import (
            PIPELINE_COURSES_DIR as courses_dir,
            PIPELINE_SHARED_STATE_DIR as legacy_dir,
        )

        for rel in local_artifact_path_candidates(clean):
            for base in (courses_dir, legacy_dir):
                candidate = base / rel
                if candidate.is_file():
                    return json.loads(candidate.read_text(encoding="utf-8"))
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Training Outline file not found: {path}",
    )
