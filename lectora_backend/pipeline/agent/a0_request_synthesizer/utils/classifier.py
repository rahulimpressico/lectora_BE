"""
LLM classifier and value resolution utilities for A0.

Azure OpenAI client/model settings live in config/llm.py.
This module only contains business logic.
"""

import difflib
import json
import logging
import re
from typing import Any

import json_repair

from ..config.llm import chat, chat_for_to
from lectora_backend.pipeline.shared_llm_config.model_registry import get_deployment
from ..prompt.classification import (
    CLASSIFICATION_PROMPT,
    CLASSIFICATIONTO_OUTLINE_PROMPT,
    GENERATE_TO_PROMPT,
    build_dynamic_to_prompt,
)

logger = logging.getLogger(__name__)

# Max words of indexed_content sent to the LLM for TO generation.
# At ~1.3 tokens/word, 100k words ≈ 130k tokens — leaves 70k tokens headroom for
# system prompt, user headers, and the model's own response within a 200k context.
_MAX_TO_INDEXED_WORDS = 100_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Remove markdown code fences that some models insert."""
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def classify_with_llm(
    title: str,
    objectives: list[str],
    content_sample: str,
    *,
    all_doc_titles: list[str] | None = None,
    heading_tree: list[dict] | None = None,
    validation_hints: str | None = None,
) -> dict:
    """Classify the course into a rule family and infer metadata via AzureOpenAI.

    Uses multiple content signals for accurate classification:
      - title: primary title (from first/primary source document)
      - all_doc_titles: titles from every uploaded source document
      - objectives: merged learning objectives from all sources
      - content_sample: representative text from all source documents
      - heading_tree: structured heading hierarchy from all sources

    The richer the signals provided, the more accurate the classification.
    """
    parts: list[str] = []

    # ── Multi-doc title signal ───────────────────────────────────────────────
    if all_doc_titles and len(all_doc_titles) > 1:
        parts.append(
            "## Source Document Titles (" + str(len(all_doc_titles)) + " files)\n"
            + "\n".join(f"- {t}" for t in all_doc_titles if t)
        )
    else:
        parts.append(f"## Course / Document Title\n{title}")

    # ── Learning objectives ──────────────────────────────────────────────────
    if objectives:
        parts.append(
            "## Learning Objectives\n"
            + "\n".join(f"- {obj}" for obj in objectives)
        )

    # ── Document heading structure (strong structural signal) ────────────────
    if heading_tree:
        heading_lines: list[str] = []
        for h in heading_tree[:80]:  # first 80 headings are sufficient
            level = int(h.get("level", 1))
            text = str(h.get("text", "")).strip()
            if text:
                indent = "  " * max(0, level - 1)
                heading_lines.append(f"[L{level}] {indent}{text}")
        if heading_lines:
            parts.append("## Document Heading Structure\n" + "\n".join(heading_lines))

    # ── Content sample ───────────────────────────────────────────────────────
    if content_sample:
        parts.append(f"## Content Sample (from all source files)\n{content_sample}")

    # ── Validation hints ─────────────────────────────────────────────────────
    if validation_hints:
        parts.append(
            "## Prior S1 validation feedback (resolve inconsistencies)\n"
            + validation_hints.strip()
        )

    user_msg = "\n\n".join(parts)

    # ── Logging ─────────────────────────────────────────────────────────────
    logger.info("[CLASSIFY] ══════════════ RULE FAMILY CLASSIFICATION ══════════════")
    logger.info("[CLASSIFY]  Primary title      : %s", title)
    if all_doc_titles:
        logger.info("[CLASSIFY]  All doc titles     : %s", all_doc_titles)
    logger.info("[CLASSIFY]  Objectives         : %d items", len(objectives))
    logger.info("[CLASSIFY]  Heading entries    : %d", len(heading_tree) if heading_tree else 0)
    logger.info(
        "[CLASSIFY]  Content sample     : %d chars",
        len(content_sample) if content_sample else 0,
    )
    logger.info("[CLASSIFY]  Sending to LLM (model=A0 → %s)…", get_deployment("A0"))

    raw = chat(CLASSIFICATION_PROMPT, user_msg)

    logger.info("[CLASSIFY]  LLM raw response   : %s", raw[:300].replace("\n", " "))

    try:
        result = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as original_exc:
        logger.warning(
            "[CLASSIFY] Invalid JSON from LLM — attempting json_repair. "
            "Raw response (first 500 chars): %r",
            raw[:500],
        )
        try:
            repaired = json_repair.repair_json(_strip_fences(raw), return_objects=True)
            if isinstance(repaired, list) and repaired and all(isinstance(i, dict) for i in repaired):
                repaired = {"sections": repaired}
            if not isinstance(repaired, dict):
                raise ValueError(
                    f"json_repair returned {type(repaired).__name__}, expected dict"
                )
            logger.info("[CLASSIFY] json_repair successfully recovered malformed classification JSON.")
            result = repaired
        except Exception as repair_exc:
            raise ValueError(
                f"LLM returned invalid JSON for course classification and repair failed. "
                f"Original error: {original_exc}. "
                f"Repair error: {repair_exc}. "
                f"Raw output (first 500 chars): {raw[:500]!r}"
            ) from original_exc

    logger.info(
        "[CLASSIFY]  ── RESULT: rule_family=%s | confidence=%.2f | topic=%s",
        result.get("rule_family"),
        float(result.get("confidence") or 0),
        result.get("topic"),
    )
    logger.info("[CLASSIFY]  ── REASONING: %s", result.get("reasoning", ""))
    logger.info("[CLASSIFY] ══════════════════════════════════════════════════════════")

    return result


def resolve_value(
    key: str, explicit: dict, rule_defaults: dict, inferred: dict
) -> tuple[Any, str]:
    """
    Resolve a value from three sources in priority order.

    Returns (value, source) where source is one of:
      'explicitly_provided', 'derived_from_rule_pack', 'inferred'
    """
    if key in explicit and explicit[key] is not None:
        return explicit[key], "explicitly_provided"
    if key in rule_defaults and rule_defaults[key] is not None:
        return rule_defaults[key], "derived_from_rule_pack"
    if key in inferred and inferred[key] is not None:
        return inferred[key], "inferred"
    return None, "unresolved"


def _parse_to_outline_json(raw: str) -> dict:
    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as original_exc:
        truncated = len(raw) < 200 or not raw.rstrip().endswith("}")
        hint = (
            " Response appears TRUNCATED — increase max_output_tokens."
            if truncated else ""
        )
        logger.warning(
            "[TO-LLM] Invalid JSON from LLM — attempting json_repair.%s "
            "Raw response (first 500 chars): %r",
            hint,
            raw[:500],
        )
        try:
            repaired = json_repair.repair_json(cleaned, return_objects=True)
            if isinstance(repaired, list) and repaired and all(isinstance(i, dict) for i in repaired):
                logger.info("[TO-LLM] json_repair returned list — wrapping as {sections: [...]}")
                repaired = {"sections": repaired}
            if not isinstance(repaired, dict):
                raise ValueError(
                    f"json_repair returned {type(repaired).__name__}, expected dict"
                )
            logger.info("[TO-LLM] json_repair successfully recovered malformed TO JSON.")
            return repaired
        except Exception as repair_exc:
            raise ValueError(
                f"LLM returned invalid JSON for TO generation and repair failed.{hint} "
                f"Original error: {original_exc}. "
                f"Repair error: {repair_exc}. "
                f"Raw output (first 500 chars): {raw[:500]!r}"
            ) from original_exc


def _build_to_user_message(
    title: str,
    objectives: list[str],
    *,
    toc_section_contents: list[dict] | None = None,
    heading_tree: list[dict] | None = None,
    pdf_toc_outline: str | None = None,
    indexed_content: str = "",
    course_difficulty: str = "intermediate",
    course_type_hint: str | None = None,
    calculated_word_count: int | None = None,
    audience: str | None = None,
    validation_hints: str | None = None,
    all_doc_titles: list[str] | None = None,
) -> str:
    """Build the user message for GENERATE_TO_PROMPT.

    Selects FORMAT A (TOC-based) when ``toc_section_contents`` is provided,
    otherwise falls back to FORMAT B (heading structure + flat indexed content).
    """
    parts: list[str] = []

    parts.append(f"## Course Difficulty\n{course_difficulty}")
    if calculated_word_count:
        parts.append(f"## Target Word Count\n{calculated_word_count}")
    if audience and audience.strip():
        parts.append(f"## Target Audience\n{audience.strip()}")
    parts.append(f"## Course Title\n{title}")
    if all_doc_titles and len(all_doc_titles) > 1:
        parts.append(
            "## Source Document Titles (ALL uploaded files)\n"
            "The course is assembled from " + str(len(all_doc_titles)) + " source document(s). "
            "Generate a course title that comprehensively covers ALL the topics across these files:\n"
            + "\n".join(f"- {t}" for t in all_doc_titles)
        )
    if objectives:
        parts.append(
            "## Learning Objectives\n"
            + "\n".join(f"- {obj}" for obj in objectives)
        )
    if course_type_hint:
        parts.append(f"## COURSE TYPE CONTEXT\n{course_type_hint}")

    if toc_section_contents:
        # FORMAT A: TOC hierarchy + per-section indexed content
        toc_lines = ["## TOC Hierarchy"]
        for sec in toc_section_contents:
            level = sec.get("level", 1)
            sec_title = sec.get("title", "")
            start = sec.get("para_idx_start")
            end = sec.get("para_idx_end")
            range_str = f"(para {start}–{end})" if start is not None else ""
            toc_lines.append(f"[L{level}] {sec_title} {range_str}".strip())
        parts.append("\n".join(toc_lines))

        content_lines = ["## Per-Section Content"]
        for sec in toc_section_contents:
            level = sec.get("level", 1)
            sec_title = sec.get("title", "")
            start = sec.get("para_idx_start")
            end = sec.get("para_idx_end")
            range_str = f"· para {start}–{end}" if start is not None else ""
            content_lines.append(f"\n### [L{level}] {sec_title} {range_str}")
            sec_content = sec.get("indexed_content", "")
            if sec_content:
                content_lines.append(sec_content)
        parts.append("\n".join(content_lines))
    else:
        # FORMAT B: heading structure (optional) + flat indexed content
        if heading_tree:
            heading_lines = ["## DOCUMENT HEADING STRUCTURE"]
            for h in heading_tree:
                level = h.get("level", 1)
                text = h.get("text", "")
                heading_lines.append(f"[L{level}] {text}")
            parts.append("\n".join(heading_lines))

        if pdf_toc_outline:
            parts.append(pdf_toc_outline.strip())

        if indexed_content:
            content_words = indexed_content.split()
            if len(content_words) > _MAX_TO_INDEXED_WORDS:
                truncated = " ".join(content_words[:_MAX_TO_INDEXED_WORDS])
                logger.warning(
                    "[TO CONTENT TRUNCATION] indexed_content truncated from %d → %d words "
                    "to stay within context limit.",
                    len(content_words),
                    _MAX_TO_INDEXED_WORDS,
                )
                indexed_content = truncated + "\n\n[CONTENT TRUNCATED — remaining paragraphs omitted to fit context window]"
            parts.append(
                f"## SOURCE DOCUMENT CONTENT (with paragraph indices)\n{indexed_content}"
            )

    if validation_hints:
        parts.append(
            "## Prior validation feedback (resolve these issues in the generated outline)\n"
            + validation_hints.strip()
        )

    return "\n\n".join(parts)


def generate_to_with_llm(
    title: str,
    objectives: list[str],
    indexed_content: str,
    *,
    heading_tree: list[dict] | None = None,
    toc_section_contents: list[dict] | None = None,
    pdf_toc_outline: str | None = None,
    course_difficulty: str = "intermediate",
    course_type_hint: str | None = None,
    duration_hours: int | float | None = None,
    calculated_word_count: int | None = None,
    audience: str | None = None,
    custom_system_prompt: str | None = None,
    validation_hints: str | None = None,
    all_doc_titles: list[str] | None = None,
) -> dict:
    """Generate a structured Timed Outline from extracted source document content.

    Used when no TO document is provided (Scenario 2). Sends structured heading
    and content data to the LLM — not raw files.

    For DOCX with Word TOC (TOC 1/2/3 styles): passes toc_section_contents (FORMAT A —
      TOC hierarchy + per-section snippets; full body not sent).
    For DOCX without TOC: passes heading_tree + indexed_content (FORMAT B).
    For PDF sources with an embedded TOC/bookmarks: passes toc_section_contents (FORMAT A).
    For PDF sources without a TOC: falls back to FORMAT B.

    System prompt priority:
      1. ``custom_system_prompt`` — FE-supplied override
      2. ``build_dynamic_to_prompt`` — when duration_hours + calculated_word_count available
      3. ``GENERATE_TO_PROMPT`` — static fallback

    Args:
        title:                 Course title extracted from the source document.
        objectives:            Learning objectives extracted from the source document.
        indexed_content:       Full [P<N>]-prefixed paragraph text from all sources.
        heading_tree:          Heading entries with para_idx (FORMAT B).
        toc_section_contents:  TOC-anchored section dicts with indexed_content (FORMAT A).
        course_difficulty:     "basic" | "intermediate" | "advanced".
        course_type_hint:      Optional domain context hint for topic selection.
        duration_hours:        Course duration (e.g. 3); used for dynamic prompt.
        calculated_word_count: Target total word count derived from duration + difficulty.
        audience:              Target audience string (e.g. "trained insurance agents").
                               Injected into the dynamic prompt and user message so the
                               LLM tailors topic selection, vocabulary, and examples.
        custom_system_prompt:  When set, takes highest priority as the system prompt.
        validation_hints:      Optional S1/S2 retry feedback to embed in the request.
        all_doc_titles:        Titles extracted from every source doc (not just the first). Enables multi-doc title synthesis.

    Returns:
        Parsed ``llm_to_outline`` dict.
    """
    # Always build the base prompt first (dynamic or static) so the JSON format
    # instructions and schema are always present.  custom_system_prompt is appended
    # as supplemental guidance — it must never replace the format contract because
    # the LLM would lose the required JSON schema and return a free-form response.
    if duration_hours is not None and calculated_word_count is not None:
        # Standard UI-driven flow: duration + difficulty + optional audience all present.
        system_prompt = build_dynamic_to_prompt(
            duration_hours=duration_hours,
            difficulty_level=course_difficulty,
            calculated_word_count=calculated_word_count,
            audience=audience,
        )
        prompt_source = (
            f"dynamic (duration={duration_hours}h, words={calculated_word_count:,}, "
            f"difficulty={course_difficulty}, audience={'set' if audience else 'none'})"
        )
    else:
        system_prompt = GENERATE_TO_PROMPT
        prompt_source = "static (GENERATE_TO_PROMPT)"

    if custom_system_prompt and custom_system_prompt.strip():
        # Append user-supplied hints after the base prompt so the JSON schema
        # constraints are preserved and the hints act as additional guidance only.
        system_prompt = (
            system_prompt
            + "\n\n"
            + "═══════════════════════════════════════════════════════════\n"
            + "ADDITIONAL INSTRUCTIONS FROM USER\n"
            + "═══════════════════════════════════════════════════════════\n"
            + custom_system_prompt.strip()
        )
        prompt_source += " + custom hints"
    # ── Pre-build diagnostics ────────────────────────────────────────────────
    raw_indexed_words = len(indexed_content.split()) if indexed_content else 0
    toc_content_words = sum(
        len((s.get("indexed_content") or "").split()) for s in (toc_section_contents or [])
    )
    fmt = "A (TOC-based)" if toc_section_contents else "B (heading + indexed)"

    logger.info(
        "[TO-LLM] ── INPUT SUMMARY ──────────────────────────────────────────"
    )
    logger.info("[TO-LLM]  Course title      : %s", title)
    logger.info("[TO-LLM]  Difficulty         : %s", course_difficulty)
    logger.info("[TO-LLM]  Duration           : %s h", duration_hours)
    logger.info("[TO-LLM]  Target word count  : %s", f"{calculated_word_count:,}" if calculated_word_count else "—")
    logger.info("[TO-LLM]  System prompt      : %s", prompt_source)
    logger.info("[TO-LLM]  Content format     : %s", fmt)
    logger.info("[TO-LLM]  Heading entries    : %d", len(heading_tree or []))
    logger.info("[TO-LLM]  TOC sections       : %d  (%d words in section bodies)", len(toc_section_contents or []), toc_content_words)
    logger.info("[TO-LLM]  indexed_content    : %d words (before truncation)", raw_indexed_words)
    if raw_indexed_words > _MAX_TO_INDEXED_WORDS:
        logger.warning(
            "[TO-LLM]  ⚠ indexed_content will be TRUNCATED: %d → %d words (~%d%% kept)",
            raw_indexed_words,
            _MAX_TO_INDEXED_WORDS,
            int(100 * _MAX_TO_INDEXED_WORDS / raw_indexed_words),
        )
    logger.info(
        "[TO-LLM] ─────────────────────────────────────────────────────────────"
    )

    user_msg = _build_to_user_message(
        title=title,
        objectives=objectives,
        toc_section_contents=toc_section_contents,
        heading_tree=heading_tree,
        pdf_toc_outline=pdf_toc_outline,
        indexed_content=indexed_content,
        course_difficulty=course_difficulty,
        course_type_hint=course_type_hint,
        calculated_word_count=calculated_word_count,
        audience=audience,
        validation_hints=validation_hints,
        all_doc_titles=all_doc_titles,
    )

    user_msg_words = len(user_msg.split())
    est_tokens = int(user_msg_words * 1.35)
    logger.info(
        "[TO-LLM]  user_msg size      : %d words (~%d tokens estimated)",
        user_msg_words,
        est_tokens,
    )
    logger.info("[TO-LLM]  Sending request to LLM (model=A0_TO → %s)…", get_deployment("A0_TO"))

    raw = chat_for_to(system_prompt, user_msg)
    resp_words = len(raw.split()) if raw else 0
    logger.info(
        "[TO-LLM]  LLM response received — %d words. Parsing TO JSON…",
        resp_words,
    )
    return _parse_to_outline_json(raw)


def map_to_to_source_indices(
    sections: list[dict],
    heading_map: list[tuple],
    total_paragraphs: int,
    *,
    paragraphs_by_source: dict[str, int] | None = None,
) -> list[dict]:
    """Add para_idx_start / para_idx_end to TO sections parsed from a TO document.

    Used in Scenario 1 (TO provided). Maps each section title to the nearest
    matching heading in the source document using fuzzy title matching, then
    assigns paragraph ranges so downstream agents can fetch raw source content.

    Args:
        sections:         List of section dicts from the parsed TO.
        heading_map:      Output of CourseDocParser.get_section_heading_map() —
                          (para_idx, heading_text, heading_level) or with a 4th
                          element: source filename when multiple DOCX files are loaded.
        total_paragraphs: Fallback paragraph count for end-of-doc sections.
        paragraphs_by_source: Optional map of filename -> paragraph count per file.

    Returns:
        Same list with para_idx_start and para_idx_end set on each section dict.
    """
    if not heading_map:
        for section in sections:
            section.setdefault("para_idx_start", None)
            section.setdefault("para_idx_end", None)
        return sections

    heading_para_indices: list[int] = []
    heading_texts: list[str] = []
    heading_sources: list[str | None] = []
    for h in heading_map:
        heading_para_indices.append(h[0])
        heading_texts.append(h[1])
        heading_sources.append(h[3] if len(h) > 3 else None)

    _NUMBER_PREFIX_RE = re.compile(r"^\d+(\.\d+)*[\s.\-:]*")

    def _clean(title: str) -> str:
        return _NUMBER_PREFIX_RE.sub("", title).lower().strip()

    def _best_match_heading_pos(section_title: str) -> int | None:
        clean = _clean(section_title)
        if not clean:
            return None
        cleaned_headings = [_clean(h) for h in heading_texts]
        matches = difflib.get_close_matches(clean, cleaned_headings, n=1, cutoff=0.4)
        if matches:
            return cleaned_headings.index(matches[0])
        return None

    result: list[dict] = []
    for i, section in enumerate(sections):
        sec = dict(section)
        pos = _best_match_heading_pos(sec.get("title", ""))
        if pos is not None:
            start = heading_para_indices[pos]
            source_file = heading_sources[pos]
            sec["para_idx_start"] = start
            if source_file:
                sec["source_document"] = source_file
            if i + 1 < len(sections):
                next_pos = _best_match_heading_pos(sections[i + 1].get("title", ""))
                next_start = heading_para_indices[next_pos] if next_pos is not None else None
                sec["para_idx_end"] = (next_start - 1) if (next_start and next_start > start) else None
            else:
                end_total = total_paragraphs - 1
                if source_file and paragraphs_by_source:
                    end_total = paragraphs_by_source.get(source_file, total_paragraphs) - 1
                sec["para_idx_end"] = end_total
        else:
            sec["para_idx_start"] = None
            sec["para_idx_end"] = None
        result.append(sec)

    return result


def classify_to_outline_with_llm(
    content_sample: str,
    *,
    validation_hints: str | None = None,
) -> dict:
    """Parse raw TO content into the structured outline format via AzureOpenAI."""
    user_msg = f"## Content\n{content_sample}"
    if validation_hints:
        user_msg += (
            "\n\n## Prior S1 validation feedback (align outline structure accordingly)\n"
            + validation_hints.strip()
        )

    raw = chat_for_to(CLASSIFICATIONTO_OUTLINE_PROMPT, user_msg)
    logger.info(
        "[TO-CLASSIFY] LLM raw response (first 300 chars): %s",
        raw[:300].replace("\n", " "),
    )
    cleaned = _strip_fences(raw)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as original_exc:
        logger.warning(
            "[TO-CLASSIFY] Invalid JSON from LLM — attempting json_repair. "
            "Raw response (first 500 chars): %r",
            raw[:500],
        )
        try:
            repaired = json_repair.repair_json(cleaned, return_objects=True)
            if isinstance(repaired, list) and repaired and all(isinstance(i, dict) for i in repaired):
                logger.info("[TO-CLASSIFY] json_repair returned list — wrapping as {sections: [...]}")
                repaired = {"sections": repaired}
            if not isinstance(repaired, dict):
                raise ValueError(
                    f"json_repair returned {type(repaired).__name__}, expected dict"
                )
            logger.info("[TO-CLASSIFY] json_repair successfully recovered malformed timed-outline JSON.")
            return repaired
        except Exception as repair_exc:
            raise ValueError(
                f"LLM returned invalid JSON for timed-outline classification and repair failed. "
                f"Original error: {original_exc}. "
                f"Repair error: {repair_exc}. "
                f"Raw output (first 500 chars): {raw[:500]!r}"
            ) from original_exc
