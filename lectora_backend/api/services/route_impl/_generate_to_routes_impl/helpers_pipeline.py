from .base import *

def _result_to_payload(
    result: A0Result,
    difficulty: str,
    source_blob_path: str | None = None,
    user_title: str | None = None,
    user_description: str | None = None,
    user_los: list[str] | None = None,
) -> dict[str, Any]:
    """Build the API response payload, enforcing user-provided fields before serializing."""
    _enforce_user_fields_in_to(
        result,
        user_title=user_title,
        user_description=user_description,
        user_los=user_los,
    )
    payload = _build_generate_to_response(
        result, difficulty, source_blob_path=source_blob_path
    ).model_dump(by_alias=True)
    _log_to_consistency(result, payload.get("to") or {})
    return payload

class TOValidationBlockedError(RuntimeError):
    """Raised when S1 blocks TO generation."""

    stage = "S1"

def _pick_s1_block_message(validation: dict[str, Any], default: str) -> str:
    """Prefer blocker issue message over non-blocker entries."""
    issues = validation.get("issues") or []
    for issue in issues:
        if str(issue.get("severity", "")).lower() == "blocker":
            msg = str(issue.get("message") or "").strip()
            if msg:
                return msg
    if issues:
        msg = str(issues[0].get("message") or "").strip()
        if msg:
            return msg
    return default

def _persist_generate_to_context(shared_state_path: str, body: GenerateTORequest) -> None:
    """Persist user requirements/context so S1 can validate with correct priority."""
    p = Path(shared_state_path).expanduser().resolve()
    if not p.is_file():
        return

    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)

    state["course_config"] = {
        "required_topics": list(body.required_topics or []),
        "learning_objectives": list(body.learning_objectives or []),
        "tone": body.tone or "",
        "depth": body.depth or "",
        "emphasis": body.emphasis or "",
        "avoid": body.avoid or "",
        "preferred_chapters": body.preferred_chapters,
        "lesson_style": body.lesson_style or "",
        "include_scenarios": body.include_scenarios,
        "include_knowledge_checks": body.include_knowledge_checks,
        "experience_level": body.experience_level or "",
        "learner_outcomes": body.learner_outcomes or "",
        "audience_notes": body.audience_notes or "",
        "course_type_hint": body.course_type_hint or "",
        "duration_hours": body.duration_hours,
        "difficulty_level": body.difficulty_level or "",
        "calculated_word_count": body.calculated_word_count,
    }
    if body.course_title:
        state["course_title_override"] = body.course_title
    if body.audience:
        state["course_audience"] = body.audience
    if body.course_description:
        state["special_instructions"] = body.course_description

    if body.source_analyses:
        state["source_file_specs"] = [
            {
                "filename": sa.source_name,
                "source_role": sa.source_role,
                "importance": sa.importance,
                "extract_hint": sa.extract_hint or "",
                "main_topics": list(sa.main_topics or []),
                "recommended_course_use": sa.recommended_course_use or "",
                "recommended_depth": sa.recommended_depth or "",
                "supports_learning_objectives": list(sa.supports_learning_objectives or []),
                "ignore_or_reduce": list(sa.ignore_or_reduce or []),
            }
            for sa in body.source_analyses
        ]

    with open(p, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False, default=str)

def _run_to_generation_pipeline(
    *,
    a0_runner: Callable[[], A0Result],
    request_body: GenerateTORequest,
    source_doc_path: str,
    difficulty: str,
    source_blob_path: str,
    user_title: str | None,
    user_description: str | None,
    user_los: list[str] | None,
    step_logger: Callable[[str, str, str | None], None] | None = None,
) -> dict[str, Any]:
    def _log(level: str, message: str, stage: str | None = None) -> None:
        if step_logger:
            step_logger(level, message, stage)

    _log("info", "A0 Agent running (single pass)…", "A0")
    result = a0_runner()
    _log("success", "A0 Agent complete: initial Topic Outline drafted.", "A0")

    _persist_generate_to_context(result.shared_state_path, request_body)

    payload = _result_to_payload(
        result,
        difficulty,
        source_blob_path=source_blob_path,
        user_title=user_title,
        user_description=user_description,
        user_los=user_los,
    )

    s1_to_result = None
    s1_to_validation: dict[str, Any] = {}
    blocked_message = "S1 blocked TO."
    for attempt in range(1, MAX_A0_A1_S1_CYCLES + 1):
        _log(
            "info",
            f"S1 Validator running: validating TO quality and requirements (attempt {attempt}/{MAX_A0_A1_S1_CYCLES})…",
            "S1",
        )
        s1_to_result = S1Validator(shared_state_path=result.shared_state_path).run(phase="to_only")
        s1_to_validation = s1_to_result.model_dump(mode="json")
        payload["s1ValidationTo"] = s1_to_validation

        if not s1_blocks(s1_to_result.status):
            if s1_to_validation.get("warnings", 0) > 0 or s1_to_validation.get("status") == "pass_with_warnings":
                _log(
                    "warn",
                    f"S1 passed with warnings ({s1_to_validation.get('warnings', 0)}) — review recommended.",
                    "S1",
                )
            _log("success", "S1 Validator passed: Topic Outline validated.", "S1")
            break

        blocked_message = _pick_s1_block_message(
            s1_to_validation,
            default="S1 blocked TO.",
        )
        if attempt < MAX_A0_A1_S1_CYCLES:
            _log(
                "warn",
                "S1 blocked TO — retrying S1 validation only (reusing cached A0 outputs, no doc re-read).",
                "S1",
            )
        else:
            _log(
                "error",
                "S1 blocked TO after retries — A0 was not re-run; cached extraction outputs were reused.",
                "S1",
            )
            err = TOValidationBlockedError(blocked_message)
            setattr(err, "payload", payload)
            setattr(err, "validation", s1_to_validation)
            raise err

    _log("info", "A1 Agent running: final TO preparation before review…", "A1")
    a1_output = a1_run(
        shared_state_path=result.shared_state_path,
        docx_path=source_doc_path,
        feedback=None,
    )
    if a1_output.status != "complete":
        raise RuntimeError(f"A1 failed during TO finalization: {a1_output.error or 'unknown error'}")
    _log("success", "A1 Agent complete: TO ready for Three Panel review.", "A1")

    _log("info", "S1 Validator running: validating A1 course_spec against rule pack…", "S1")
    s1_a1_result = S1Validator(shared_state_path=result.shared_state_path).run(phase="a1_only")
    s1_a1_validation = s1_a1_result.model_dump(mode="json")
    payload["s1ValidationA1"] = s1_a1_validation
    payload["s1Validation"] = s1_a1_validation

    if s1_blocks(s1_a1_result.status):
        blocked_message = _pick_s1_block_message(
            s1_a1_validation,
            default="S1 blocked A1 output.",
        )
        _log("error", f"S1 blocked A1 output: {blocked_message}", "S1")
        err = TOValidationBlockedError(blocked_message)
        setattr(err, "payload", payload)
        setattr(err, "validation", s1_a1_validation)
        raise err

    if s1_a1_validation.get("warnings", 0) > 0 or s1_a1_validation.get("status") == "pass_with_warnings":
        _log(
            "warn",
            f"S1 A1 validation passed with warnings ({s1_a1_validation.get('warnings', 0)}) — review recommended.",
            "S1",
        )
    else:
        _log("success", "S1 Validator passed: A1 course_spec validated.", "S1")

    return payload

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
