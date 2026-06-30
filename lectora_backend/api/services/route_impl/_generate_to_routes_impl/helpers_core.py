from .base import *

from .helpers_pipeline import _result_to_payload, TOValidationBlockedError, _pick_s1_block_message, _persist_generate_to_context, _run_to_generation_pipeline, _parse_course_topic

def _set_preview_trace_context(
    *,
    route_name: str,
    course_title: str | None = None,
    source_refs: list[str] | None = None,
) -> None:
    """Tag preview-only LLM routes with a readable trace context for Langfuse."""
    from lectora_backend.pipeline.shared_llm_config.tracer import set_run_context

    resolved_title = (course_title or "").strip() or route_name
    run_id = f"{route_name.lower()}-{sanitize_segment(resolved_title) or 'preview'}"
    set_run_context(
        run_id,
        resolved_title,
        source_refs=source_refs or [],
    )

def _run_ingestion_background(
    file_bytes: bytes,
    filename: str,
    document_id: str,
) -> None:
    """Background task: write bytes to a temp file, run ingestion, clean up."""
    import asyncio
    import tempfile
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
                os.unlink(tmp_path)
            except OSError:
                pass

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

    # Normalise then extract sections using shared outline-selection helpers.
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

def _enforce_user_fields_in_to(
    result: "A0Result",
    *,
    user_title: str | None,
    user_description: str | None,
    user_los: list[str] | None,
) -> None:
    """Guarantee user-provided values appear verbatim in llm_to_outline (memory + disk).

    Belt-and-suspenders: the LLM prompt already instructs the model to copy these
    values, but LLMs occasionally rephrase them.  This function enforces them
    unconditionally after A0 completes so the file, the in-memory result, and the
    API response are always identical to the user's onboarding input.

    Mutates ``result.llm_to_outline`` in-place (dict reference) so that
    ``_build_generate_to_response`` and ``_log_to_consistency`` see the same values
    without needing to re-read the file.
    """
    if not user_title and not user_description and not user_los:
        return

    # 1. Patch in-memory dict (reference shared with _build_generate_to_response)
    mem = result.llm_to_outline
    if isinstance(mem, dict):
        if user_title:
            mem["course_title"] = user_title
        if user_description:
            mem["description"] = user_description
        if user_los:
            mem["learning_objectives"] = list(user_los)

    # 2. Patch persisted file so llm_to_outline.json matches the API response.
    #    A2 reads llm_to_outline_classification from shared_state (which is set
    #    from this file by A0), so the values flow into content generation too.
    try:
        outline_path = Path(result.output_files.llm_to_outline)
        if not outline_path.exists():
            logger.warning(
                "[generate-to][enforce] llm_to_outline.json not found at %s — skipping disk patch",
                outline_path,
            )
            return
        with open(outline_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        inner: dict = payload.get("llm_to_outline") or {}
        if user_title:
            inner["course_title"] = user_title
        if user_description:
            inner["description"] = user_description
        if user_los:
            inner["learning_objectives"] = list(user_los)
        payload["llm_to_outline"] = inner
        with open(outline_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
        logger.info(
            "[generate-to][enforce] User values written to llm_to_outline.json — "
            "title=%r | desc=%d chars | LOs=%d",
            user_title,
            len(user_description or ""),
            len(user_los or []),
        )
    except Exception as exc:
        logger.warning("[generate-to][enforce] Could not write to llm_to_outline.json: %s", exc)

def _log_to_consistency(result: "A0Result", response_to: dict[str, Any]) -> None:
    """Compare the in-memory LLM TO against the persisted file and the API response.

    Logs a warning for any mismatch so data inconsistencies are visible immediately.
    Expected: LLM output == llm_to_outline.json == API response (TO Response).
    """
    # Read persisted file
    try:
        with open(result.output_files.llm_to_outline, encoding="utf-8") as fh:
            file_payload = json.load(fh)
        file_inner: dict = file_payload.get("llm_to_outline") or {}
    except Exception as exc:
        logger.warning("[generate-to][consistency] Could not read llm_to_outline.json: %s", exc)
        file_inner = {}

    mem_inner: dict = result.llm_to_outline or {}
    checks = {
        "course_title": (
            mem_inner.get("course_title"),
            file_inner.get("course_title"),
            response_to.get("course_name"),
        ),
        "description": (
            mem_inner.get("description"),
            file_inner.get("description"),
            response_to.get("description"),
        ),
        "learning_objectives": (
            mem_inner.get("learning_objectives"),
            file_inner.get("learning_objectives"),
            response_to.get("learning_objectives"),
        ),
    }
    all_ok = True
    for field, (mem_val, file_val, resp_val) in checks.items():
        if mem_val != file_val or mem_val != resp_val:
            logger.warning(
                "[generate-to][consistency] MISMATCH on '%s':\n"
                "  LLM output : %s\n"
                "  File       : %s\n"
                "  API resp   : %s",
                field,
                str(mem_val)[:200],
                str(file_val)[:200],
                str(resp_val)[:200],
            )
            all_ok = False
    if all_ok:
        logger.info("[generate-to][consistency] ✓ LLM output == file == API response for title/description/LOs")
