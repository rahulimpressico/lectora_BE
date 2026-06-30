from __future__ import annotations

from .base import *

from .pipeline_create_job import create_job

def _write_to_override(to_data: dict[str, Any], temp_dir: Path) -> Path:
    """Write user-edited TO as a JSON file A0 loads directly (no LLM call)."""
    path = temp_dir / "user_edited_to.json"
    payload = {"llm_to_outline": llm_outline_from_to_data(to_data)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

def _persist_difficulty(shared_state_path: str, difficulty: str) -> None:
    """Inject course_difficulty into shared_state.json for downstream agents."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["course_difficulty"] = difficulty.strip().lower()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)

def _persist_source_file_paths(shared_state_path: str, paths: list[str]) -> None:
    """Store source file local paths in shared_state for A2 chunk-based retrieval."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["source_file_paths"] = paths
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)

def _persist_source_file_specs(shared_state_path: str, specs: list[dict]) -> None:
    """Store per-file source specs (extract_hint) in shared_state for A2."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["source_file_specs"] = specs
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)

def _persist_audience(shared_state_path: str, audience: str) -> None:
    """Store target audience in shared_state for A2 prompt calibration."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["course_audience"] = audience.strip()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)

def _persist_special_instructions(shared_state_path: str, instructions: str) -> None:
    """Store user special instructions in shared_state for A2 prompt injection."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["special_instructions"] = instructions.strip()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)

def _persist_course_title(shared_state_path: str, title: str) -> None:
    """Store user-provided course title in shared_state so A2 uses it verbatim."""
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    state["course_title"] = title.strip()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)

def _persist_course_config(shared_state_path: str, course_config: "CourseConfigPayload") -> None:
    """Store wizard onboarding fields in shared_state for A2 dynamic prompt construction.

    Only prompt-guidance fields (tone, depth, emphasis, etc.) are written here.
    course_title, course_description, and learning_objectives are intentionally
    NOT overwritten — A2 reads them directly from llm_to_outline_classification,
    which holds the exact TO the LLM generated (or the user's edited TO).
    """
    p = Path(shared_state_path)
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    config_dict: dict[str, Any] = {}
    # Prompt-guidance fields only — do NOT include course_title or course_description.
    if course_config.experience_level:
        config_dict["experience_level"] = course_config.experience_level
    if course_config.learner_outcomes and course_config.learner_outcomes.strip():
        config_dict["learner_outcomes"] = course_config.learner_outcomes.strip()
    if course_config.audience_notes and course_config.audience_notes.strip():
        config_dict["audience_notes"] = course_config.audience_notes.strip()
    if course_config.learning_objectives:
        config_dict["learning_objectives"] = list(course_config.learning_objectives)
    if course_config.tone and course_config.tone.strip():
        config_dict["tone"] = course_config.tone.strip()
    if course_config.depth and course_config.depth.strip():
        config_dict["depth"] = course_config.depth.strip()
    if course_config.emphasis and course_config.emphasis.strip():
        config_dict["emphasis"] = course_config.emphasis.strip()
    if course_config.avoid and course_config.avoid.strip():
        config_dict["avoid"] = course_config.avoid.strip()
    if course_config.include_scenarios is not None:
        config_dict["include_scenarios"] = course_config.include_scenarios
    if course_config.include_knowledge_checks is not None:
        config_dict["include_knowledge_checks"] = course_config.include_knowledge_checks
    state["course_config"] = config_dict
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)

def _run_pipeline_sync(
    job_id: str,
    study_guide_path: str,
    timed_outline_path: str | None,
    to_override: dict[str, Any] | None,
    difficulty: str,
    source_file_paths: list[str] | None = None,
    audience: str = "",
    special_instructions: str | None = None,
    source_file_specs: list[dict] | None = None,
    course_config: "CourseConfigPayload | None" = None,
) -> tuple[str | None, str | None]:
    """Execute the full pipeline synchronously.  Returns (shared_state_path, study_guide_docx_path)."""
    store = get_local_course_job_store()
    # Temp dir is used only for ephemeral input files (e.g. user_edited_to.json).
    # All pipeline output artifacts go to the persistent shared_state directory
    # so they land in the same location as direct pipeline.py runs.
    temp_input_dir = Path(tempfile.mkdtemp(prefix=f"lectora_job_{job_id[:8]}_"))

    try:
        return _run_pipeline_inner(
            job_id, study_guide_path, timed_outline_path, to_override, difficulty,
            temp_input_dir, source_file_paths, audience, special_instructions,
            source_file_specs=source_file_specs,
            course_config=course_config,
        )
    finally:
        # Ephemeral input dir is tiny (user_edited_to.json only); clean up always.
        shutil.rmtree(temp_input_dir, ignore_errors=True)

def _run_pipeline_inner(
    job_id: str,
    study_guide_path: str,
    timed_outline_path: str | None,
    to_override: dict[str, Any] | None,
    difficulty: str,
    temp_input_dir: Path,
    source_file_paths: list[str] | None = None,
    audience: str = "",
    special_instructions: str | None = None,
    source_file_specs: list[dict] | None = None,
    course_config: "CourseConfigPayload | None" = None,
) -> tuple[str | None, str | None]:
    """Inner pipeline body, called from _run_pipeline_sync after temp dir setup."""
    store = get_local_course_job_store()

    def log(level: str, message: str, stage: str | None = None) -> None:
        store.append_log(job_id, level, message, stage)
        logger.info("[%s] [%s] %s", job_id[:8], stage or "pipeline", message)

    _set_trace_context_for_job(job_id, study_guide_path=study_guide_path)

    # Resolve effective TO path for first gate cycle
    effective_to_path: str | None = None
    if to_override and isinstance(to_override, dict):
        override_path = _write_to_override(to_override, temp_input_dir)
        effective_to_path = str(override_path)
        log("info", "Using your reviewed Training Outline — skipping outline regeneration")
    elif timed_outline_path:
        effective_to_path = timed_outline_path

    # ── Content-generation entry flow: A1 → A2 → S2 ──────────────────────
    # TO validation (A0 → A1 → S1) already happens in /documents/generate-to.
    # Here we prepare shared state once from the finalized TO and proceed.
    shared_state_path: str | None = None

    # Compute per-job output slug once — each run gets its own isolated dir:
    # pipeline/shared_state/{course_slug}/{job_id}/
    _job_rec = store.get(job_id)
    course_slug = sanitize_course_slug(_job_rec.course_title if _job_rec else "course")
    job_output_slug = f"{course_slug}/{job_id}"
    artifact_dir = _PIPELINE_COURSES_DIR / course_slug / job_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "logs").mkdir(parents=True, exist_ok=True)
    store.set_artifact_dir(job_id, str(artifact_dir))

    if store.is_cancelled(job_id):
        raise RuntimeError("Cancelled")

    log("info", "Preparing finalized TO context for content generation…", "A0")
    store.start_stage(job_id, "A0")

    # Build shared state once from the finalized TO payload/file.
    to_path_for_cycle = effective_to_path
    _sg_ext = Path(study_guide_path).suffix.lower()
    _a0_docx_paths: list[str] = [] if _sg_ext == ".pdf" else [study_guide_path]
    _a0_pdf_paths: list[str] = [study_guide_path] if _sg_ext == ".pdf" else []

    a0_result = A0RequestSynthesizer(
        docx_paths=_a0_docx_paths,
        pdf_paths=_a0_pdf_paths,
        to_outline_doc_path=to_path_for_cycle,
        output_dir=str(_PIPELINE_COURSES_DIR),
        course_difficulty=difficulty,
        course_output_slug=job_output_slug,
    ).run()
    shared_state_path = a0_result.shared_state_path
    store.set_temp_dir(job_id, str(Path(shared_state_path).parent))
    _persist_difficulty(shared_state_path, difficulty)

    if source_file_paths:
        _persist_source_file_paths(shared_state_path, source_file_paths)
    if source_file_specs:
        _persist_source_file_specs(shared_state_path, source_file_specs)
    if audience:
        _persist_audience(shared_state_path, audience)
    if special_instructions:
        _persist_special_instructions(shared_state_path, special_instructions)
    if course_config:
        _persist_course_config(shared_state_path, course_config)
    _job_title = (store.get(job_id) or _job_rec)
    if _job_title and _job_title.course_title:
        _persist_course_title(shared_state_path, _job_title.course_title)
    log("success", "Finalized TO context loaded", "A0")
    store.complete_stage(job_id, "A0", "PASS")

    log("info", "A1 processing finalized TO for content generation…", "A1")
    store.start_stage(job_id, "A1")
    a1_output = a1_run(
        shared_state_path=shared_state_path,
        docx_path=study_guide_path,
        feedback=None,
    )
    if getattr(a1_output, "status", "") != "complete":
        store.complete_stage(job_id, "A1", "FAILED")
        raise RuntimeError("A1 failed while preparing finalized TO for content generation")
    log("success", "A1 complete — TO prepared for content generation", "A1")
    store.complete_stage(job_id, "A1", "PASS")

    if store.is_cancelled(job_id):
        raise RuntimeError("Cancelled")

    # ── Section Mapper ────────────────────────────────────────────────────
    assert shared_state_path is not None
    log("info", "Organizing course sections and mapping lessons to the training outline…", "SECTION_MAPPER")
    store.start_stage(job_id, "SECTION_MAPPER")
    section_mapper_run(shared_state_path=shared_state_path)
    log("success", "Course sections organized and lesson mapping complete", "SECTION_MAPPER")
    store.complete_stage(job_id, "SECTION_MAPPER", "PASS")

    # ── KC Planner ────────────────────────────────────────────────────────
    log("info", "Planning interactive knowledge check placement…", "KC_PLANNER")
    store.start_stage(job_id, "KC_PLANNER")
    kc_result = kc_planner_run(shared_state_path=shared_state_path)
    kc_count = kc_result.get("kc_count", 0)
    log("success", f"Knowledge check placement complete — {kc_count} interactive checks planned", "KC_PLANNER")
    store.complete_stage(job_id, "KC_PLANNER", "PASS")

    # ── A2 → S2 loop ──────────────────────────────────────────────────────
    log("info", "Generating comprehensive content for each lesson…", "A2")
    store.start_stage(job_id, "A2")

    a2_feedback: str | None = None
    s2_result: Any = None
    final_docx_path: str | None = None

    for a2_cycle in range(1, MAX_A2_S2_CYCLES + 1):
        if store.is_cancelled(job_id):
            raise RuntimeError("Cancelled")
        if a2_cycle > 1:
            log("info", "Refining course content based on quality feedback…", "A2")

        a2_result = A2ContentGenerator(
            shared_state_path=shared_state_path,
            docx_path=study_guide_path,
            render_docx=False,
            course_difficulty=difficulty,
            feedback=a2_feedback,
            source_file_paths=source_file_paths,
        ).run()
        log(
            "success",
            f"Content generation complete — {a2_result.stats.generated} lessons written "
            f"({a2_result.stats.total_words:,} words)",
            "A2",
        )

        log("info", "Checking content quality, accuracy, and completeness…", "S2")
        store.start_stage(job_id, "S2")
        s2_result = S2Validator(shared_state_path).run()

        if not s2_blocks(s2_result.status):
            log("success", "Content quality review passed", "S2")
            store.complete_stage(job_id, "A2", "PASS")
            store.complete_stage(job_id, "S2", "PASS")
            break

        blocker_issues = [
            {"message": getattr(i, "message", str(i)), "field": getattr(i, "field", "")}
            for i in (getattr(s2_result, "issues", []) or [])
            if getattr(i, "severity", "") == "blocker"
        ]
        store.complete_stage(job_id, "S2", "BLOCKED", blockers=blocker_issues, retry_attempt=a2_cycle)

        if a2_cycle < MAX_A2_S2_CYCLES:
            a2_feedback = format_s2_feedback(s2_result)
            log("warn", "Content quality issues found — regenerating with improvements…", "S2")
        else:
            log("error", "Content quality could not be resolved after multiple attempts", "S2")
            store.complete_stage(job_id, "A2", "FAILED")

    # Render study_guide.docx only when S2 cleared
    if s2_result and not s2_blocks(s2_result.status):
        log("info", "Assembling your final course document…", "A2")
        final_docx_path = render_study_guide_from_state(shared_state_path=shared_state_path)
        log("success", "Your course document is ready", "A2")
    else:
        log("error", "Course document could not be assembled — quality gate blocked", "A2")

    return shared_state_path, final_docx_path

async def _run_pipeline_background(
    job_id: str,
    study_guide_path: str,
    timed_outline_path: str | None,
    to_override: dict[str, Any] | None,
    difficulty: str,
    source_file_paths: list[str] | None = None,
    audience: str = "",
    special_instructions: str | None = None,
    source_file_specs: list[dict] | None = None,
    course_config: "CourseConfigPayload | None" = None,
) -> None:
    store = get_local_course_job_store()
    store.update_status(job_id, LocalJobStatus.PROCESSING)
    store.set_input_docx(job_id, study_guide_path)
    store.append_log(job_id, "info", "Course generation started", None)

    def _sync() -> tuple[str | None, str | None]:
        return _run_pipeline_sync(
            job_id, study_guide_path, timed_outline_path, to_override, difficulty,
            source_file_paths, audience, special_instructions,
            source_file_specs=source_file_specs,
            course_config=course_config,
        )

    try:
        shared_state_path, study_guide_docx = await asyncio.to_thread(_sync)
        store.complete_job(
            job_id,
            shared_state_path=shared_state_path,
            study_guide_path=study_guide_docx,
        )

        # Sync all pipeline JSON/DOCX artifacts to Azure Blob Storage.
        if shared_state_path:
            from lectora_backend.core.local_artifact_sync import sync_local_artifacts_to_azure

            job = store.get(job_id)
            course_title = job.course_title if job else "course"
            sync_result = await asyncio.to_thread(
                sync_local_artifacts_to_azure,
                job_id=job_id,
                course_title=course_title,
                shared_state_path=shared_state_path,
                study_guide_path=study_guide_docx,
            )
            if sync_result.get("skipped"):
                logger.debug(
                    "[%s] Azure artifact sync skipped: %s",
                    job_id[:8],
                    sync_result.get("reason"),
                )
            elif sync_result.get("uploaded", 0) > 0:
                blob_root = sync_result.get("blobRoot")
                if blob_root:
                    store.set_azure_blob_root(job_id, blob_root)
                store.append_log(
                    job_id,
                    "success",
                    f"Uploaded {sync_result['uploaded']} artifact(s) to Azure "
                    f"({sync_result.get('container')}/{blob_root})",
                    None,
                )
            if sync_result.get("errors"):
                store.append_log(
                    job_id,
                    "warn",
                    f"Some artifacts failed to upload to Azure ({len(sync_result['errors'])} error(s))",
                    None,
                )

        store.append_log(job_id, "success", "Your course has been generated successfully", None)
    except Exception as exc:
        if str(exc) == "Cancelled" or store.is_cancelled(job_id):
            store.cancel_job(job_id, reason="Cancelled")
            store.append_log(job_id, "warn", "Course generation cancelled", None)
            logger.info("[%s] Pipeline cancelled", job_id[:8])
        else:
            logger.exception("[%s] Pipeline failed: %s", job_id[:8], exc)
            store.fail_job(
                job_id,
                error={"message": str(exc), "type": type(exc).__name__},
            )
            store.append_log(job_id, "error", f"Generation failed: {exc}", None)
    finally:
        unregister_local_pipeline(job_id)
        store.release_slot()
