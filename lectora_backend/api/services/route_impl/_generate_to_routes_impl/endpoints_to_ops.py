from .base import *

async def load_to_from_path(
    path: str = Query(..., description="Blob path from upload or artifact browse"),
    source: Literal["uploads", "artifacts"] = Query(default="uploads"),
) -> GenerateTOResponse:
    payload = _read_json_blob(path, source)
    # FE-format blobs (written by POST /documents/save-to) contain "to" + "rules"
    # directly — skip the LLM→FE conversion step.
    if isinstance(payload.get("to"), dict) and isinstance(payload.get("rules"), dict):
        return GenerateTOResponse(to=payload["to"], rules=payload["rules"])
    return build_fe_to_response_from_llm_outline(_unwrap_llm_outline(payload))

async def save_to(body: SaveTORequest) -> SaveTOResponse:
    """
    Overwrite the blob at ``blobPath`` with the user-edited Training Outline.

    Written in FE format ``{ "to": {...}, "rules": {...} }`` so that
    ``GET /documents/load-to`` can detect and return it directly without
    passing it through the LLM→FE conversion pipeline.

    Both the local filesystem cache and Azure Blob Storage (when configured)
    are updated so that the backend always reflects the latest user edits.
    """
    blob_path = body.blob_path.strip()
    if not blob_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="blobPath is required.",
        )

    # Resolve relative path — strip container prefix if present
    rel = blob_path
    for prefix in (f"{UPLOADED_DOCUMENTS_PREFIX}/", f"{UPLOADED_DOCUMENTS_PREFIX}"):
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
            break

    payload = {"to": body.to}
    if body.rules is not None:
        payload["rules"] = body.rules  # type: ignore[assignment]
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    content_bytes = content.encode("utf-8")

    # Always write to local filesystem (dev mode + Azure cache)
    local_path = _UPLOAD_ROOT / rel
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(content, encoding="utf-8")
    logger.info("[save-to] Wrote TO to local path → %s", local_path)

    # Write to Azure Blob Storage when configured
    if _azure_storage_ready():
        try:
            _uploads_blob_repo().upload_bytes(
                rel,
                content_bytes,
                content_type="application/json",
            )
            logger.info("[save-to] Uploaded TO to Azure → %s", rel)
        except Exception as exc:
            # Local write succeeded; log the Azure failure but don't fail the request.
            logger.warning("[save-to] Azure upload failed (local copy saved): %s", exc)

    return SaveTOResponse(blob_path=blob_path)

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
CONSTRAINT 1 — Count: write 4–6 objectives by default.
  More than 6 dilutes focus; fewer than 4 fails to cover the course scope.
  EXCEPTION: if the user provides regeneration guidance requesting more or fewer
  objectives, honour that request even if it falls outside the 4–6 default range.

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
  4. Is the total count between 4 and 6 (or within the range the user requested)?
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
    if body.current_objectives:
        co_lines = [
            "\nCURRENT OBJECTIVES (the list the user sees right now — modify these "
            "according to the regeneration guidance below; do not start from scratch):",
        ]
        for i, obj in enumerate(body.current_objectives, 1):
            co_lines.append(f"  {i}. {obj}")
        input_parts.append("\n".join(co_lines))
    if body.regeneration_prompt and body.regeneration_prompt.strip():
        regen = body.regeneration_prompt.strip()
        current_count = len(body.current_objectives)
        count_hint = (
            f" (the user currently has {current_count} objectives; "
            f"adjust the total accordingly)"
            if current_count
            else ""
        )
        input_parts.append(
            f"REGENERATION GUIDANCE — highest priority{count_hint}: {regen}"
        )

    if body.required_topics:
        rt_lo_lines = [
            "\nREQUIRED TOPICS — Every generated objective must be traceable to at least one of these:",
        ]
        for topic in body.required_topics:
            rt_lo_lines.append(f"  • {topic}")
        rt_lo_lines.append(
            "\nEnsure each required topic is covered by at least one learning objective. "
            "Do not omit any topic from this list."
        )
        input_parts.append("\n".join(rt_lo_lines))

    if body.source_analyses:
        sa_lines = [
            "\nSOURCE ANALYSIS (use this to align objectives to the actual source content):",
        ]
        for sa in body.source_analyses:
            sa_lines.append(f"\n[{sa.source_name}] role={sa.source_role}")
            if sa.extract_hint:
                sa_lines.append(f"  What to get: {sa.extract_hint}")
            if sa.main_topics:
                sa_lines.append(f"  Topics: {', '.join(sa.main_topics)}")
            if sa.supports_learning_objectives:
                sa_lines.append("  Suggested LOs from this source:")
                for lo in sa.supports_learning_objectives:
                    sa_lines.append(f"    - {lo}")
            if sa.ignore_or_reduce:
                sa_lines.append(f"  Avoid/reduce: {', '.join(sa.ignore_or_reduce)}")
        sa_lines.extend([
            "\nWeighting rules for LO selection:",
            "  - Base objectives on each source's extraction focus and primary topics",
            "  - Supporting sources may contribute to individual objectives only if highly relevant",
            "  - Do NOT write objectives for topics listed under Avoid/reduce above",
        ])
        input_parts.append("\n".join(sa_lines))

    user_msg = (
        "\n".join(input_parts)
        or "Generate general learning objectives for this training course."
    )
    _set_preview_trace_context(
        route_name="lo-gen",
        course_title=body.course_title,
    )

    try:
        def _call_llm() -> str:
            return llm_chat(system_prompt, user_msg, config, "LO_GEN")

        raw = await asyncio.to_thread(_call_llm)
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

async def suggest_required_topics(
    body: SuggestRequiredTopicsRequest,
) -> SuggestRequiredTopicsResponse:
    """Analyse course metadata and return 8–15 required topics the course must cover.

    Called automatically when the user lands on the Required Topics step with an
    empty topic list, and also when the user clicks 'Regenerate Suggestions'.
    """
    from lectora_backend.pipeline.shared_llm_config.llm import LLMConfig, chat as llm_chat
    from lectora_backend.pipeline.shared_llm_config.model_registry import get_deployment

    config = LLMConfig(
        deployment=get_deployment("A0_TO"),
        max_tokens=1024,
        response_format={"type": "json_object"},
    )

    system_prompt = """\
You are an expert instructional designer specialising in corporate, regulatory, \
and compliance training courses. Analyse the provided course metadata and return \
the specific topics that the course MUST cover in order to meet its objectives and \
regulatory requirements.

TOPIC RULES:
- Return 8–15 topics. Fewer than 8 is too sparse; more than 15 dilutes focus.
- Each topic must be concrete and specific (e.g. "COBRA qualifying events and \
election deadlines", not just "COBRA").
- Topics should reflect regulatory mandates, must-know concepts, and compliance \
areas a learner will be tested on in certification exams.
- Keep each topic concise: 5–15 words.
- Do NOT repeat topics or group unrelated concepts under one item.

Return a JSON object with this exact structure:
{"required_topics": ["Topic 1", "Topic 2", ...]}\
"""

    input_parts: list[str] = []
    if body.course_title:
        input_parts.append(f"Course title: {body.course_title}")
    if body.course_description:
        input_parts.append(f"Description: {body.course_description}")
    if body.course_type:
        input_parts.append(f"Course type: {body.course_type}")
    if body.course_duration:
        input_parts.append(f"Duration: {body.course_duration}")
    if body.target_audience:
        input_parts.append(f"Target audience: {body.target_audience}")
    if body.skill_level:
        input_parts.append(f"Skill level: {body.skill_level}")
    if body.learner_outcomes:
        input_parts.append(f"Desired learner outcomes: {body.learner_outcomes}")

    user_msg = (
        "\n".join(input_parts)
        or "Suggest required topics for a standard regulatory training course."
    )
    _set_preview_trace_context(
        route_name="suggest-required-topics",
        course_title=body.course_title,
    )

    try:
        def _call_llm() -> str:
            return llm_chat(system_prompt, user_msg, config, "SUGGEST_TOPICS")

        raw = await asyncio.to_thread(_call_llm)
        data = json.loads(raw)
        topics: list[str] = data.get("required_topics") or []
        if not isinstance(topics, list):
            topics = []
        topics = [str(t).strip() for t in topics if t]
        logger.info("[suggest-required-topics] Generated %d topics", len(topics))
        return SuggestRequiredTopicsResponse(required_topics=topics)
    except json.JSONDecodeError as exc:
        logger.warning("[suggest-required-topics] JSON parse error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LLM returned malformed JSON. Please try again.",
        ) from exc
    except Exception as exc:
        logger.exception("[suggest-required-topics] LLM call failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to suggest required topics: {exc}",
        ) from exc


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
    _set_preview_trace_context(
        route_name="suggest-structure",
        course_title=body.course_title,
    )

    try:
        def _call_llm() -> str:
            return llm_chat(system_prompt, user_msg, config, "SUGGEST_STRUCTURE")

        raw = await asyncio.to_thread(_call_llm)
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

async def suggest_course_type(body: SuggestCourseTypeRequest) -> SuggestCourseTypeResponse:
    """Use the LLM classifier to recommend a rule family based on course metadata.

    Accepts title, description, audience, and learning objectives — no uploaded files
    required. Only called when the user explicitly clicks "Suggested by AI" in the UI;
    never triggered automatically.

    Returns the rule family key (e.g. ``insurance_ce``), its display label
    (e.g. ``Insurance CE``), a confidence score, and a one-sentence reasoning.
    """
    from lectora_backend.pipeline.agent.a0_request_synthesizer.step_02_classification.utils.classifier import (
        classify_with_llm,
    )

    # Build the content sample from the available wizard metadata.
    content_parts: list[str] = []
    if body.course_description.strip():
        content_parts.append(body.course_description.strip())
    if body.target_audience.strip():
        content_parts.append(f"Target audience: {body.target_audience.strip()}")
    content_sample = "\n".join(content_parts)

    _set_preview_trace_context(
        route_name="suggest-course-type",
        course_title=body.course_title,
    )

    try:
        result: dict = await asyncio.to_thread(
            classify_with_llm,
            body.course_title or "Untitled Course",
            body.learning_objectives,
            content_sample,
        )
    except Exception as exc:
        logger.exception("[suggest-course-type] Classification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to suggest course type: {exc}",
        ) from exc

    rule_family_key = str(result.get("rule_family") or "insurance_ce").strip()
    if rule_family_key not in RULE_PACKS:
        logger.warning(
            "[suggest-course-type] LLM returned unknown rule_family %r — falling back to insurance_ce",
            rule_family_key,
        )
        rule_family_key = "insurance_ce"

    rule_family_label: str = RULE_PACKS[rule_family_key].get("family", rule_family_key)
    confidence = float(result.get("confidence") or 0.0)
    reasoning = str(result.get("reasoning") or "").strip()

    logger.info(
        "[suggest-course-type] Suggested rule_family=%s label=%r confidence=%.2f",
        rule_family_key,
        rule_family_label,
        confidence,
    )

    return SuggestCourseTypeResponse(
        rule_family=rule_family_key,
        rule_family_label=rule_family_label,
        confidence=confidence,
        reasoning=reasoning,
    )

async def revise_to(body: ReviseTORequest) -> ReviseTOResponse:
    """
    Send the current Training Outline JSON and a user revision prompt to the LLM.

    The LLM revises the existing outline in place — it does NOT regenerate from
    source documents. Only the changes described in ``revisionPrompt`` are applied;
    everything else is preserved.

    Returns the revised TO as ``{ "to": { ... } }``.
    """
    from lectora_backend.pipeline.shared_llm_config.llm import LLMConfig, chat as llm_chat
    from lectora_backend.pipeline.shared_llm_config.model_registry import get_deployment

    revision_prompt = (body.revision_prompt or "").strip()
    if not revision_prompt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="revisionPrompt is required and must not be empty.",
        )

    current_to_json = json.dumps(body.current_to, indent=2)

    user_message = (
        f"Current Training Outline:\n{current_to_json}\n\n"
        f"---\n\n"
        f"Revision instructions:\n{revision_prompt}"
    )

    config = LLMConfig(
        deployment=get_deployment("A0_TO"),
        max_tokens=16_384,
        response_format={"type": "json_object"},
    )

    logger.info(
        "[revise-to] Starting TO revision | prompt_length=%d | to_sections=%s",
        len(revision_prompt),
        len(body.current_to.get("sections", body.current_to.get("modules", []))),
    )

    try:
        def _call_llm() -> str:
            return llm_chat(_REVISE_TO_SYSTEM_PROMPT, user_message, config, "REVISE_TO")

        raw = await asyncio.to_thread(_call_llm)

        # Strip accidental markdown fences if the model ignored RULE 1
        stripped = raw.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
            stripped = re.sub(r"\n?```$", "", stripped.rstrip())

        revised_to = json.loads(stripped)

        if not isinstance(revised_to, dict):
            raise ValueError("LLM returned a non-object JSON value.")

        logger.info("[revise-to] Revision complete")
        return ReviseTOResponse(to=revised_to)

    except json.JSONDecodeError as exc:
        logger.warning("[revise-to] JSON parse error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The LLM returned malformed JSON. Please try again.",
        ) from exc
    except Exception as exc:
        logger.exception("[revise-to] LLM call failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to revise the Training Outline: {exc}",
        ) from exc
