from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from lectora_backend.api.schemas.generate_to_schemas import GenerateTOResponse
from lectora_backend.pipeline.rule_pack_config.rule_packs import RULE_PACKS, resolve_rule_pack
from lectora_backend.pipeline.agent.a0_request_synthesizer.step_04_post_processing.utils.outline_metrics import (
    compute_course_totals,
    get_difficulty_factor,
)

logger = logging.getLogger(__name__)

_SECTION_KEYS: tuple[str, ...] = (
    "sections", "lessons", "modules", "table_of_contents", "recommended_scope",
)
_WRAPPER_KEYS: tuple[str, ...] = (
    "outline", "course_outline", "timed_outline", "to", "result",
)
_STRIP_FROM_RULES = {
    "difficulty_levels",
    "sample_courses_available",
    "exam_file_format_samples",
    "unique_artifacts",
    "new_course_requested",
}


def find_rule_family_key(family_display_name: str) -> str:
    """
    Map a rule-pack ``family`` display name (e.g. "Insurance CE") back to its
    RULE_PACKS dict key (e.g. "insurance_ce").
    Falls back to the first key if nothing matches.
    """
    for key, pack in RULE_PACKS.items():
        if pack.get("family") == family_display_name:
            return key
    return family_display_name.lower().replace(" ", "_")


def safe_int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def safe_float(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def clean_sections(sections: list[dict]) -> list[dict]:
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
            "word_count": safe_int(s.get("word_count")),
            "duration_minutes": safe_int(s.get("minutes")),
            "credit_hours": safe_float(s.get("credit_hour") or s.get("credit_hours")),
            "interactive_elements": s.get("interactive_elements") or [],
        })
    return cleaned


def clean_rule_pack(pack: dict | None) -> dict:
    """Strip internal-only keys from the resolved rule pack."""
    if not pack:
        return {}
    return {k: v for k, v in pack.items() if k not in _STRIP_FROM_RULES}


def unwrap_llm_outline(raw: dict) -> dict:
    """Normalise saved JSON payloads to the inner llm_to_outline dict."""
    if not raw:
        return {}
    inner = raw.get("llm_to_outline")
    return inner if isinstance(inner, dict) else raw


def normalise_llm_outline(outline: dict) -> dict:
    """Apply title-alias normalisation to a resolved outline dict."""
    if not outline.get("course_title") and outline.get("generated_course_title"):
        outline = dict(outline)
        outline["course_title"] = outline["generated_course_title"]
    return outline


def pick_sections(outline: dict) -> tuple[list[dict], dict]:
    """Try every known section key; also try one level of wrapper keys."""
    for key in _SECTION_KEYS:
        sections = outline.get(key)
        if sections and isinstance(sections, list):
            return sections, outline

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


def extract_outline_sections(llm_outline: dict) -> tuple[list[dict], dict, dict]:
    """Return *(sections, totals, resolved_outline)* from heterogeneous TO JSON."""
    outline = normalise_llm_outline(unwrap_llm_outline(llm_outline))
    sections, resolved = pick_sections(outline)
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
    sections, totals, outline = extract_outline_sections(llm_outline)
    _ = totals

    family_key = rule_family_key or "insurance_ce"
    if shared_state_path and Path(shared_state_path).is_file():
        try:
            with open(shared_state_path, encoding="utf-8") as fh:
                state = json.load(fh)
            request_spec = state.get("request_spec") or {}
            rule_cls = request_spec.get("rule_classification") or {}
            family_display = rule_cls.get("family") or ""
            if family_display:
                family_key = find_rule_family_key(family_display)
            difficulty = (state.get("course_difficulty") or difficulty).strip().lower()
        except Exception:
            pass

    resolved_pack = resolve_rule_pack(family_key, difficulty)
    rules = clean_rule_pack(resolved_pack)

    cleaned_sections = clean_sections(sections)
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

