from __future__ import annotations

import logging
from typing import Any

from .deterministic import _check_required_topics_deterministic, _required_topics_to_issues
from .models import MissingTopic, ValidationIssue, ValidationResult
from .semantic import SemanticValidator, _finalize_result

logger = logging.getLogger("lectora_backend.pipeline.agent.s1_validator.utils.ai_checks")


def _trim_text(value: str | None, *, max_chars: int = 280) -> str:
    text = (value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


def _section_digest_from_course_spec(course_spec: dict[str, Any]) -> list[dict[str, Any]]:
    digest: list[dict[str, Any]] = []
    for idx, section in enumerate(course_spec.get("sections", []) or []):
        digest.append(
            {
                "index": idx + 1,
                "id": section.get("id") or section.get("section_id") or f"sec_{idx+1}",
                "level": section.get("level", 1),
                "title": _trim_text(section.get("heading") or section.get("title") or "", max_chars=180),
                "word_count": section.get("word_count"),
                "has_knowledge_check": bool(section.get("has_knowledge_check")),
                "maps_to_objectives": section.get("maps_to_objectives") or [],
            }
        )
    return digest


def _section_digest_from_llm_outline(shared_state: dict[str, Any]) -> list[dict[str, Any]]:
    llm_outline = shared_state.get("llm_to_outline_classification") or {}
    sections = llm_outline.get("sections") or llm_outline.get("course_outline", {}).get("sections") or []
    digest: list[dict[str, Any]] = []
    for idx, section in enumerate(sections):
        subtopics = section.get("subtopics") or section.get("topics") or []
        digest.append(
            {
                "index": idx + 1,
                "id": section.get("id") or section.get("section_id") or f"sec_{idx+1}",
                "level": section.get("level", 1),
                "title": _trim_text(section.get("title") or section.get("heading") or "", max_chars=180),
                "word_count": section.get("word_count"),
                "has_knowledge_check": bool(section.get("has_knowledge_check")),
                "maps_to_objectives": section.get("maps_to_objectives") or [],
                "subtopics": [
                    _trim_text(item.get("title") if isinstance(item, dict) else str(item), max_chars=120)
                    for item in subtopics
                ],
            }
        )
    return digest


def _collect_user_requirements(shared_state: dict[str, Any]) -> dict[str, Any]:
    request_spec = shared_state.get("request_spec") or {}
    course_meta = request_spec.get("course_metadata") or {}
    course_config = shared_state.get("course_config") or {}

    required_topics = (
        course_config.get("required_topics")
        or request_spec.get("required_topics")
        or shared_state.get("required_topics")
        or []
    )

    source_hints = []
    for spec in (shared_state.get("source_file_specs") or []):
        hint = (spec.get("extract_hint") or "").strip()
        if hint:
            source_hints.append(
                {
                    "source_name": spec.get("filename") or spec.get("blob_path") or "source",
                    "source_role": spec.get("source_role") or "",
                    "importance": spec.get("importance") or "",
                    "extract_hint": hint,
                    "main_topics": spec.get("main_topics") or [],
                    "recommended_course_use": spec.get("recommended_course_use") or "",
                    "recommended_depth": spec.get("recommended_depth") or "",
                    "supports_learning_objectives": spec.get("supports_learning_objectives") or [],
                    "ignore_or_reduce": spec.get("ignore_or_reduce") or [],
                }
            )

    return {
        "audience": course_meta.get("audience") or "",
        "course_type": course_meta.get("course_type") or "",
        "topic": course_meta.get("topic") or "",
        "category": course_meta.get("category") or "",
        "difficulty_level": course_meta.get("difficulty_level") or shared_state.get("course_difficulty") or "",
        "course_title_override": shared_state.get("course_title_override") or "",
        "learning_objectives": shared_state.get("extracted_inputs", {}).get("learning_objectives") or [],
        "user_learning_objectives": course_config.get("learning_objectives") or [],
        "required_topics": required_topics,
        "special_instructions": shared_state.get("special_instructions") or "",
        "emphasis": course_config.get("emphasis") or "",
        "avoid": course_config.get("avoid") or "",
        "tone": course_config.get("tone") or "",
        "depth": course_config.get("depth") or "",
        "preferred_chapters": course_config.get("preferred_chapters"),
        "lesson_style": course_config.get("lesson_style") or "",
        "experience_level": course_config.get("experience_level") or "",
        "learner_outcomes": course_config.get("learner_outcomes") or "",
        "audience_notes": course_config.get("audience_notes") or "",
        "course_type_hint": course_config.get("course_type_hint") or "",
        "duration_hours": course_config.get("duration_hours"),
        "calculated_word_count": course_config.get("calculated_word_count"),
        "include_scenarios": course_config.get("include_scenarios"),
        "include_knowledge_checks": course_config.get("include_knowledge_checks"),
        "source_hints": source_hints,
    }


def _has_user_requirements(requirements: dict[str, Any]) -> bool:
    return any(
        [
            bool(requirements.get("required_topics")),
            bool(requirements.get("special_instructions")),
            bool(requirements.get("emphasis")),
            bool(requirements.get("avoid")),
            bool(requirements.get("source_hints")),
            bool(requirements.get("learning_objectives")),
            bool(requirements.get("user_learning_objectives")),
            bool(requirements.get("tone")),
            bool(requirements.get("depth")),
            bool(requirements.get("preferred_chapters")),
            bool(requirements.get("lesson_style")),
            bool(requirements.get("experience_level")),
            bool(requirements.get("learner_outcomes")),
            bool(requirements.get("audience_notes")),
            bool(requirements.get("course_type_hint")),
            bool(requirements.get("duration_hours")),
            bool(requirements.get("calculated_word_count")),
            requirements.get("include_scenarios") is not None,
            requirements.get("include_knowledge_checks") is not None,
        ]
    )


def _fallback_issues(error: Exception) -> list[dict[str, Any]]:
    return [
        ValidationIssue(
            field="s1_ai_validator",
            expected="AI semantic validation result",
            found=str(error),
            severity="warning",
            message=(
                "AI semantic validation could not be completed. "
                "Only deterministic S1 checks were applied for this run."
            ),
            rule_source="s1_ai_validator",
            failure_reason="LLM call or response parsing failed after retries.",
            remediation=(
                "Retry semantic validation. If the issue persists, verify model deployment and "
                "response-format compatibility."
            ),
        ).model_dump(exclude_none=True)
    ]


def _result_to_issue_dicts(result: ValidationResult) -> list[dict[str, Any]]:
    issues = [issue.model_dump(exclude_none=True) for issue in result.issues]

    if result.summary:
        issues.append(
            ValidationIssue(
                field="s1_ai_validator.summary",
                expected="Semantic TO validation summary",
                found=result.summary,
                severity="info",
                message=result.summary,
                rule_source="s1_ai_validator",
            ).model_dump(exclude_none=True)
        )

    issues.append(
        ValidationIssue(
            field="s1_ai_validator.metrics",
            expected="Scores and confidence computed",
            found={
                "overall_score": round(result.overall_score, 2),
                "coverage_score": round(result.coverage_score, 2),
                "sequence_score": round(result.sequence_score, 2),
                "relevance_score": round(result.relevance_score, 2),
                "completeness_score": round(result.completeness_score, 2),
                "confidence": round(result.confidence, 3),
                "status": result.status,
            },
            severity="info",
            message="AI semantic validation scores computed.",
            rule_source="s1_ai_validator.metrics",
        ).model_dump(exclude_none=True)
    )

    if result.retry_required:
        issues.append(
            ValidationIssue(
                field="s1_ai_validator.retry",
                expected="retry_required=false",
                found=True,
                severity="warning",
                message="AI validator requested TO regeneration.",
                rule_source="s1_ai_validator.retry",
                remediation=result.retry_prompt,
            ).model_dump(exclude_none=True)
        )
    return issues


def _merge_required_topic_findings(
    result: ValidationResult,
    det_issues: list[ValidationIssue],
    det_missing: list[MissingTopic],
) -> None:
    if not det_issues and not det_missing:
        return

    existing_fields = {i.field for i in result.issues}
    for issue in det_issues:
        if issue.field not in existing_fields:
            result.issues.append(issue)
            existing_fields.add(issue.field)

    existing_topics = {mt.topic.lower() for mt in result.missing_topics}
    for mt in det_missing:
        if mt.topic.lower() not in existing_topics:
            result.missing_topics.append(mt)
            existing_topics.add(mt.topic.lower())

    _finalize_result(result)


def run_ai_outline_checks(
    *,
    shared_state: dict[str, Any],
    course_spec: dict[str, Any],
    rule_pack: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    _ = course_spec
    requirements = _collect_user_requirements(shared_state)
    has_user_reqs = _has_user_requirements(requirements)
    priority_rule = (
        "User requirements override rule pack whenever both are present."
        if has_user_reqs
        else "No explicit user requirements found; validate using rule pack only."
    )

    sections = _section_digest_from_llm_outline(shared_state)
    if not sections:
        logger.warning(
            "[S1][AI] No A0 TO sections found in llm_to_outline_classification; "
            "semantic validation will run with empty outline context."
        )

    required_topics: list[str] = requirements.get("required_topics") or []
    coverage_results = _check_required_topics_deterministic(required_topics, sections)

    det_issues: list[ValidationIssue] = []
    det_missing: list[MissingTopic] = []
    if coverage_results:
        det_issues, det_missing = _required_topics_to_issues(coverage_results)

    required_topics_precheck = [
        {
            "topic": c.topic,
            "status": c.status,
            "match_pct": int(c.matched_fraction * 100),
            "found_in_sections": c.found_in_sections[:4],
        }
        for c in coverage_results
    ]
    missing_required = [c.topic for c in coverage_results if c.status == "missing"]
    partial_required = [c.topic for c in coverage_results if c.status == "partial"]

    n_missing = len(missing_required)
    n_partial = len(partial_required)
    if n_missing or n_partial:
        logger.warning(
            "[S1][AI] Required-topics pre-check: %d missing, %d partial out of %d requested.",
            n_missing,
            n_partial,
            len(required_topics),
        )
    else:
        logger.info(
            "[S1][AI] Required-topics pre-check: all %d required topics covered.", len(required_topics)
        )

    payload = {
        "validation_priority": priority_rule,
        "has_user_requirements": has_user_reqs,
        "user_requirements": requirements,
        "rule_pack": {
            "family": rule_pack.get("family"),
            "version": rule_pack.get("version"),
            "content_rules": rule_pack.get("content_rules", {}),
            "assessment_rules": rule_pack.get("assessment_rules", {}),
            "compliance_elements": rule_pack.get("compliance_elements", {}),
        },
        "course_outline": {
            "title": requirements.get("course_title_override")
            or shared_state.get("extracted_inputs", {}).get("title")
            or "",
            "total_sections": len(sections),
            "sections": sections,
        },
        "required_topics_precheck": {
            "total_requested": len(required_topics),
            "missing_count": n_missing,
            "partial_count": n_partial,
            "coverage": required_topics_precheck,
            "instruction": (
                "The deterministic pre-check above already flagged the topics below. "
                "Do NOT contradict these findings. For each missing/partial topic, "
                "produce a corresponding issue and add it to missing_topics. "
                f"Missing: {missing_required}. Partial: {partial_required}."
            )
            if (n_missing or n_partial)
            else "All required topics detected by pre-check.",
        },
        "frontend_input_contract": {
            "source": "POST /documents/generate-to",
            "a0_only_validation": True,
            "notes": (
                "Validate only against A0-generated TO and user inputs persisted from FE; "
                "ignore A1 structure for this semantic pass."
            ),
        },
    }

    try:
        validator = SemanticValidator()
        result = validator.run(payload=payload, priority_rule=priority_rule)
        _merge_required_topic_findings(result, det_issues, det_missing)
        return _result_to_issue_dicts(result), priority_rule
    except Exception as exc:
        logger.exception("[S1][AI] Outline semantic validation failed: %s", exc)
        fallback = _fallback_issues(exc)
        for issue in det_issues:
            fallback.append(issue.model_dump(exclude_none=True))
        return fallback, priority_rule


__all__ = ["run_ai_outline_checks"]
