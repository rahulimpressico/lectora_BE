from __future__ import annotations

from .base import *

def _regenerate_special_section_sync(job_id: str, section_id: str) -> str:
    """Regenerate Conclusion (LLM) or return the stored user-provided Overview verbatim."""
    from lectora_backend.pipeline.agent.a2_content_generator.main import (
        _build_course_conclusion,
    )

    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
    if not job or not job.shared_state_path:
        raise ValueError(f"Job {job_id} not found or has no shared state")

    with open(job.shared_state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)

    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2") or {}

    if section_id == "course-overview":
        # The overview comes from the TO's description — the single source of truth.
        # Read from llm_to_outline_classification first, then fall back to what A2 stored.
        llm_to: dict = shared_state.get("llm_to_outline_classification") or {}
        new_content = (
            (llm_to.get("description") or "")
            or (a2_output.get("course_description") or "")
        ).strip()
        if not new_content:
            raise ValueError("No course description found in TO or A2 output.")
    elif section_id == "course-conclusion":
        course_title: str = a2_output.get("course_title") or "Untitled Course"
        extracted: dict = shared_state.get("extracted_inputs", {}) or {}
        learning_objectives: list[str] = [str(lo) for lo in (extracted.get("learning_objectives") or [])]
        content_sample: str = extracted.get("content_sample", "") or ""
        sections: list[dict] = a2_output.get("sections") or []
        new_content = _build_course_conclusion(
            course_title,
            content_sample=content_sample,
            learning_objectives=learning_objectives,
            generated_sections=sections,
        )
        if not new_content:
            raise ValueError(f"LLM returned empty content for section '{section_id}'")
    else:
        raise ValueError(f"Cannot regenerate special section '{section_id}'")

    _persist_section_text(job_id, section_id, new_content)
    return new_content

def _set_trace_context_for_job(job_id: str, *, study_guide_path: str | None = None) -> None:
    """Tag LLM traces with run_id + doc_name so costing can attribute per-document spend."""
    from lectora_backend.pipeline.shared_llm_config.tracer import set_run_context

    store = get_local_course_job_store()
    job = store.get(job_id)
    doc_name = ""
    source_refs: list[str] = []
    if study_guide_path:
        doc_name = Path(study_guide_path).stem
        source_refs.append(study_guide_path)
    if job and job.input_docx_path and job.input_docx_path not in source_refs:
        source_refs.append(job.input_docx_path)
    if not doc_name and job and job.course_title:
        doc_name = sanitize_course_slug(job.course_title)
    if not doc_name:
        doc_name = job_id[:8]
    set_run_context(job_id, doc_name, source_refs=source_refs)

def _rewrite_section_sync(job_id: str, section_id: str, current_content: str, user_prompt: str) -> str:
    """Rewrite section content using LLM with user-provided instructions."""
    _set_trace_context_for_job(job_id)
    from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
    from lectora_backend.pipeline.agent.a2_content_generator.config.llm import COURSE_DESCRIPTION_CONFIG

    instructions = user_prompt.strip() or "Improve clarity and flow while preserving the original meaning."
    system = (
        "You are an expert course content writer. "
        "Rewrite the provided course section following the user's instructions exactly. "
        "Preserve all factual content and learning points. "
        "Output only the rewritten section content — no preamble, labels, or explanation."
    )
    user_msg = f"CURRENT SECTION CONTENT:\n{current_content}\n\nREWRITE INSTRUCTIONS:\n{instructions}"
    raw = llm_chat(system, user_msg, config=COURSE_DESCRIPTION_CONFIG, agent="A2")
    new_content = (raw or "").strip()
    if not new_content:
        return current_content
    _persist_section_text(job_id, section_id, new_content)
    return new_content

def _improve_tone_sync(job_id: str, section_id: str, current_content: str, tone_prompt: str) -> str:
    """Adjust tone and style of section content using LLM."""
    _set_trace_context_for_job(job_id)
    from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
    from lectora_backend.pipeline.agent.a2_content_generator.config.llm import COURSE_DESCRIPTION_CONFIG

    tone = tone_prompt.strip() or "Professional, clear, and engaging"
    system = (
        "You are an expert course content editor specialising in tone and style. "
        "Rewrite the provided course section in the requested tone and style. "
        "Preserve all factual content and learning points. "
        "Output only the revised content — no preamble, labels, or explanation."
    )
    user_msg = f"CURRENT SECTION CONTENT:\n{current_content}\n\nDESIRED TONE/STYLE:\n{tone}"
    raw = llm_chat(system, user_msg, config=COURSE_DESCRIPTION_CONFIG, agent="A2")
    new_content = (raw or "").strip()
    if not new_content:
        return current_content
    _persist_section_text(job_id, section_id, new_content)
    return new_content

def _summarize_section_sync(job_id: str, section_id: str, current_content: str) -> str:
    """Summarize section content to a concise version preserving all key learning points."""
    _set_trace_context_for_job(job_id)
    from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
    from lectora_backend.pipeline.agent.a2_content_generator.config.llm import COURSE_DESCRIPTION_CONFIG

    system = (
        "You are an expert course content editor. "
        "Summarize the following course section into a concise version that retains all key "
        "learning points, facts, and concepts. Target roughly 40–60% of the original length. "
        "Use clear, direct language. "
        "Output only the summarized content — no preamble, labels, or explanation."
    )
    user_msg = f"COURSE SECTION CONTENT:\n{current_content}"
    raw = llm_chat(system, user_msg, config=COURSE_DESCRIPTION_CONFIG, agent="editor")
    new_content = (raw or "").strip()
    if not new_content:
        return current_content
    _persist_section_text(job_id, section_id, new_content)
    return new_content

def _expand_section_sync(job_id: str, section_id: str, current_content: str) -> str:
    """Expand section content with additional depth, examples, and elaboration."""
    _set_trace_context_for_job(job_id)
    from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
    from lectora_backend.pipeline.agent.a2_content_generator.config.llm import COURSE_DESCRIPTION_CONFIG

    system = (
        "You are an expert course content writer. "
        "Expand the following course section by adding more depth, concrete examples, "
        "and elaboration on key concepts. Maintain the same educational tone and style. "
        "Only deepen what is already present — do not introduce unrelated topics. "
        "Output only the expanded content — no preamble, labels, or explanation."
    )
    user_msg = f"COURSE SECTION CONTENT:\n{current_content}"
    raw = llm_chat(system, user_msg, config=COURSE_DESCRIPTION_CONFIG, agent="editor")
    new_content = (raw or "").strip()
    if not new_content:
        return current_content
    _persist_section_text(job_id, section_id, new_content)
    return new_content

def _simplify_section_sync(job_id: str, section_id: str, current_content: str) -> str:
    """Simplify section content using plainer language and shorter sentences."""
    _set_trace_context_for_job(job_id)
    from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
    from lectora_backend.pipeline.agent.a2_content_generator.config.llm import COURSE_DESCRIPTION_CONFIG

    system = (
        "You are an expert course content editor. "
        "Simplify the following course section by using plainer language, shorter sentences, "
        "and a more accessible writing style. Avoid jargon where possible; "
        "when technical terms are necessary, briefly explain them. "
        "Preserve all factual content and learning points exactly. "
        "Output only the simplified content — no preamble, labels, or explanation."
    )
    user_msg = f"COURSE SECTION CONTENT:\n{current_content}"
    raw = llm_chat(system, user_msg, config=COURSE_DESCRIPTION_CONFIG, agent="editor")
    new_content = (raw or "").strip()
    if not new_content:
        return current_content
    _persist_section_text(job_id, section_id, new_content)
    return new_content

def _regenerate_section_sync(job_id: str, section_id: str, current_content: str) -> str:
    """
    Regenerate a single section's content via LLM and persist to shared_state.
    Returns the new plain-text content for the section.
    """
    # Propagate trace context so A2 LLM calls are attributed to this job in Langfuse.
    _set_trace_context_for_job(job_id)

    # Special sections (overview / conclusion) use their own dedicated generators
    if section_id in ("course-overview", "course-conclusion"):
        return _regenerate_special_section_sync(job_id, section_id)
    if section_id == "course-learning-objectives":
        return current_content  # LOs are derived from source; not regeneratable here

    from lectora_backend.pipeline.agent.a2_content_generator.utils.content_writer import (
        generate_lesson,
    )
    from lectora_backend.pipeline.agent.a2_content_generator.utils.source_chunker import (
        extract_full_section_text,
        load_doc_paragraphs,
    )
    from lectora_backend.pipeline.rule_pack_config.rule_packs import resolve_rule_pack

    store = get_local_course_job_store()
    job = _resolve_job_with_filesystem(store, job_id)
    if not job or not job.shared_state_path:
        raise ValueError(f"Job {job_id} not found or has no shared state")

    with open(job.shared_state_path, encoding="utf-8") as fh:
        shared_state = json.load(fh)

    a2_output: dict = shared_state.get("agent_outputs", {}).get("A2") or {}
    sections: list[dict] = a2_output.get("sections") or []

    # Find target section — try exact section_id match, then positional "sec_N" fallback
    target_sec, _ = _find_a2_section(sections, section_id)
    if not target_sec:
        # The section may be a synthetic L1 parent injected by _inject_missing_lesson_parent_sections
        # (these are not stored in shared_state but ARE assigned IDs in get_course_content).
        # Promote it into the real sections list so it can be regenerated and persisted.
        expanded = _inject_missing_lesson_parent_sections(sections)
        synth_sec, _ = _find_a2_section(expanded, section_id)
        if synth_sec:
            _promote_synthetic_parent(sections, synth_sec)
            target_sec, _ = _find_a2_section(sections, section_id)
        else:
            raise ValueError(f"Section '{section_id}' not found in A2 output")

    extracted = shared_state.get("extracted_inputs", {}) or {}
    learning_objectives: list[str] = [str(lo) for lo in (extracted.get("learning_objectives") or [])]

    rule_family: str | None = (
        shared_state.get("request_spec", {}).get("rule_classification", {}).get("family")
    )
    difficulty: str = shared_state.get("course_difficulty") or "intermediate"
    rule_pack = resolve_rule_pack(rule_family, difficulty) if rule_family else None
    if not rule_pack:
        raise ValueError(f"Cannot resolve rule pack for family '{rule_family}'")

    # Locate original para_start / para_end from enriched_sections (Section Mapper output).
    # Match by the section's own A1 section_id first, then fall back to title/heading match
    # so parent-overview sections (which have empty section_id) are handled correctly.
    enriched_sections: list[dict] = (
        shared_state.get("agent_outputs", {})
        .get("section_map", {})
        .get("enriched_sections", [])
    )
    section_real_id: str = target_sec.get("section_id") or ""
    section_heading: str = target_sec.get("heading") or ""
    para_start = para_end = 0
    for lesson in enriched_sections:
        for sub in lesson.get("subtopics") or []:
            matched = (
                (section_real_id and sub.get("id") == section_real_id)
                or (section_heading and sub.get("title") == section_heading)
            )
            if matched:
                para_start = int(sub.get("para_start") or 0)
                para_end = int(sub.get("para_end") or 0)
                break

    # Extract source text from original input doc (DOCX or PDF) when available
    source_text = ""
    if job.input_docx_path and Path(job.input_docx_path).exists() and (para_start or para_end):
        try:
            doc_paragraphs = load_doc_paragraphs(
                job.input_docx_path,
                shared_state_path=job.shared_state_path,
            )
            source_text = extract_full_section_text(
                doc_paragraphs, para_start=para_start, para_end=para_end
            )
        except Exception:
            source_text = ""

    # Fall back to current FE content if no source doc available
    if not source_text:
        source_text = current_content or ""

    word_count = int(target_sec.get("word_count") or 400)
    lesson_title = target_sec.get("outline_lesson") or ""
    lesson_entry = next(
        (l for l in enriched_sections if l.get("title") == lesson_title),
        {"title": lesson_title, "word_count": str(word_count), "minutes": "5"},
    )

    subtopic_spec = {
        "heading": target_sec.get("heading", ""),
        "target_word_count": word_count,
        "source_text": source_text,
        "has_knowledge_check": bool(target_sec.get("has_knowledge_check")),
        "maps_to_objectives": target_sec.get("maps_to_objectives") or [],
        "subtopics": target_sec.get("subtopics") or [],
        "interactive_elements": [],
        "image_count": 0,
        "target_minutes": 0,
    }

    results = generate_lesson(
        lesson=lesson_entry,
        subtopic_specs=[subtopic_spec],
        learning_objectives=learning_objectives,
        prior_summary="",
        rule_pack=rule_pack,
        lesson_wc=word_count,
    )

    if not results:
        raise ValueError("LLM returned no result for section regeneration")

    new_data = results[0]
    new_body = new_data.get("body_paragraphs") or []
    new_wc = int(new_data.get("word_count") or word_count)

    # Persist updated section to shared_state (target_sec is already the matched object)
    target_sec["body_paragraphs"] = new_body
    target_sec["word_count"] = new_wc
    target_sec["status"] = "regenerated"

    a2_output["sections"] = sections
    shared_state["agent_outputs"]["A2"] = a2_output
    with open(job.shared_state_path, "w", encoding="utf-8") as fh:
        json.dump(shared_state, fh, indent=2, default=str)

    return _paragraphs_to_text(new_body)
