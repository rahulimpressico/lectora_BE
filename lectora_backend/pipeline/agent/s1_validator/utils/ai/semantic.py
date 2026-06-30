from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import ValidationError

from lectora_backend.pipeline.shared_llm_config.llm import LLMConfig, chat as llm_chat
from lectora_backend.pipeline.shared_llm_config.model_registry import get_deployment

from .models import (
    MAX_LLM_RETRIES,
    RETRY_BACKOFF_SECONDS,
    BaseValidator,
    DependencyIssue,
    MissingTopic,
    ObjectiveMapping,
    Recommendation,
    ValidationIssue,
    ValidationResult,
)

try:
    import json_repair  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    json_repair = None

logger = logging.getLogger("lectora_backend.pipeline.agent.s1_validator.utils.ai_checks")

SYSTEM_PROMPT = (
    "You are S1 Validator for Topic Outline quality gates. "
    "Return ONLY valid JSON and follow the response schema exactly."
)

_A0_NON_BLOCKING_FIELD_TOKENS = (
    "maps_to_objectives",
    "learning_objective_mapping",
    "knowledge_check",
    "exam",
    "assessment",
)

VALIDATION_RULES = [
    "User requirement alignment",
    "Rule pack compliance",
    "Topic completeness",
    "Topic sequencing",
    "Duplicate detection",
    "Missing critical topics",
    "Dependency ordering",
    "Topic granularity",
    "Difficulty progression",
    "Industry best practices",
    "Hallucinated topics",
    "Source hint coverage",
    "Learning objective coverage",
    "Section balance",
    "Practical vs theoretical balance",
    "Assessment readiness",
    "Naming consistency",
    "Course duration alignment",
]

SEVERITY_POLICY = (
    "Severity policy:\n"
    "- blocker: major quality/completeness failure; should stop pipeline\n"
    "- warning: notable quality gap; can proceed with review\n"
    "- info: minor observation"
)

RESPONSE_SCHEMA = """
{
  "summary": "short sentence",
  "overall_score": 0,
  "coverage_score": 0,
  "sequence_score": 0,
  "relevance_score": 0,
  "completeness_score": 0,
  "confidence": 0,
  "status": "PASS|FAIL",
  "issues": [
    {
      "field": "path",
      "severity": "blocker|warning|info",
      "message": "issue",
      "expected": "expected condition",
      "found": "actual finding",
      "rule_source": "user_requirements|rule_pack|sequence|coverage|relevance|clarity",
      "failure_reason": "why it matters",
      "remediation": "what to change"
    }
  ],
  "recommendations": [
    {"title": "what to improve", "detail": "how to improve", "priority": "high|medium|low"}
  ],
  "missing_topics": [
    {"topic": "topic", "reason": "why missing", "severity": "high|medium|low"}
  ],
  "duplicates": ["topic A", "topic B"],
  "dependency_issues": [
    {"topic": "dependent topic", "missing_prerequisite": "prerequisite", "reason": "why order is wrong"}
  ],
  "learning_objective_mapping": [
    {"objective": "objective text", "status": "covered|partial|missing", "evidence": "short proof"}
  ],
  "retry_required": false,
  "retry_prompt": ""
}
""".strip()


def _build_system_prompt() -> str:
    rules = "\n".join(f"{idx + 1}) {rule}" for idx, rule in enumerate(VALIDATION_RULES))
    return "\n\n".join(
        [
            SYSTEM_PROMPT,
            "Validate all checks:\n" + rules,
            "Return JSON with this exact schema:\n" + RESPONSE_SCHEMA,
            SEVERITY_POLICY,
            (
                "Pass criteria (must enforce):\n"
                "- PASS if blocker_issues == 0 AND overall_score >= 85 AND confidence >= 0.8\n"
                "- otherwise FAIL and retry_required=true with retry_prompt guidance"
            ),
            (
                "A0 TO validation constraints:\n"
                "- Do NOT require final exam blueprint/question-count in A0 outline.\n"
                "- Do NOT require explicit maps_to_objectives arrays in A0 sections.\n"
                "- Do NOT require has_knowledge_check flags to be true in every section at A0 stage.\n"
                "- If any of the above are missing, emit warning/info (not blocker)."
            ),
        ]
    )


def _safe_json_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if json_repair is not None:
            repaired = json_repair.repair_json(raw, return_objects=True)
            if repaired is not None:
                return repaired
        raise


def _coerce_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _clamp_score(value: Any, *, max_value: float = 100.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(max_value, numeric))


def _count_by_severity(issues: list[ValidationIssue], severity: str) -> int:
    return sum(1 for issue in issues if issue.severity == severity)


def _normalize_issue(raw: dict[str, Any], idx: int) -> ValidationIssue:
    raw_sev = str(raw.get("severity", "warning")).lower()
    severity_map = {
        "blocker": "blocker",
        "blocked": "blocker",
        "critical": "blocker",
        "major": "blocker",
        "warning": "warning",
        "warn": "warning",
        "info": "info",
    }
    severity = severity_map.get(raw_sev, "warning")
    field = str(raw.get("field") or f"ai_outline_validation.{idx}")
    message = str(raw.get("message") or "AI validator found an outline issue.")

    if severity == "blocker":
        lower_field = field.lower()
        lower_msg = message.lower()
        if any(token in lower_field or token in lower_msg for token in _A0_NON_BLOCKING_FIELD_TOKENS):
            severity = "warning"

    return ValidationIssue(
        field=field,
        expected=str(raw.get("expected") or "Requirement satisfied"),
        found=raw.get("found", "Not satisfied"),
        severity=severity,  # type: ignore[arg-type]
        message=message,
        rule_source=str(raw.get("rule_source") or "s1_ai_validator"),
        failure_reason=raw.get("failure_reason"),
        remediation=raw.get("remediation"),
    )


def _coerce_recommendation(raw: Any) -> Recommendation:
    if isinstance(raw, str):
        return Recommendation(title=raw, detail="", priority="medium")
    if isinstance(raw, dict):
        return Recommendation.model_validate(raw)
    return Recommendation(title=str(raw), detail="", priority="low")


def _coerce_missing_topic(raw: Any) -> MissingTopic:
    if isinstance(raw, str):
        return MissingTopic(topic=raw, reason="", severity="medium")
    if isinstance(raw, dict):
        return MissingTopic.model_validate(raw)
    return MissingTopic(topic=str(raw), reason="", severity="low")


def _coerce_dependency_issue(raw: Any) -> DependencyIssue:
    if isinstance(raw, str):
        return DependencyIssue(topic=raw, missing_prerequisite="", reason="")
    if isinstance(raw, dict):
        return DependencyIssue.model_validate(raw)
    return DependencyIssue(topic=str(raw), missing_prerequisite="", reason="")


def _coerce_objective_mapping(raw: Any) -> ObjectiveMapping:
    if isinstance(raw, str):
        return ObjectiveMapping(objective=raw, status="partial", evidence="")
    if isinstance(raw, dict):
        return ObjectiveMapping.model_validate(raw)
    return ObjectiveMapping(objective=str(raw), status="partial", evidence="")


def _to_validation_result(raw_data: dict[str, Any]) -> ValidationResult:
    raw_issues = _coerce_list(raw_data.get("issues"))
    issues = [
        _normalize_issue(issue, idx)
        for idx, issue in enumerate(raw_issues)
        if isinstance(issue, dict)
    ]

    recommendations = [
        _coerce_recommendation(item)
        for item in _coerce_list(raw_data.get("recommendations"))
    ]
    missing_topics = [
        _coerce_missing_topic(item)
        for item in _coerce_list(raw_data.get("missing_topics"))
    ]
    dependency_issues = [
        _coerce_dependency_issue(item)
        for item in _coerce_list(raw_data.get("dependency_issues"))
    ]
    objective_mapping = [
        _coerce_objective_mapping(item)
        for item in _coerce_list(raw_data.get("learning_objective_mapping"))
    ]

    duplicates = [
        str(item).strip()
        for item in _coerce_list(raw_data.get("duplicates"))
        if str(item).strip()
    ]

    try:
        return ValidationResult(
            summary=str(raw_data.get("summary") or "").strip(),
            overall_score=_clamp_score(raw_data.get("overall_score")),
            coverage_score=_clamp_score(raw_data.get("coverage_score")),
            sequence_score=_clamp_score(raw_data.get("sequence_score")),
            relevance_score=_clamp_score(raw_data.get("relevance_score")),
            completeness_score=_clamp_score(raw_data.get("completeness_score")),
            confidence=_clamp_score(raw_data.get("confidence"), max_value=1.0),
            status="PASS" if str(raw_data.get("status", "FAIL")).strip().upper() == "PASS" else "FAIL",
            issues=issues,
            recommendations=recommendations,
            missing_topics=missing_topics,
            duplicates=duplicates,
            dependency_issues=dependency_issues,
            learning_objective_mapping=objective_mapping,
            retry_required=bool(raw_data.get("retry_required", False)),
            retry_prompt=str(raw_data.get("retry_prompt") or "").strip(),
        )
    except ValidationError:
        logger.exception("[S1][AI] ValidationResult parsing failed; using minimal fallback.")
        return ValidationResult(
            summary="Semantic validation result could not be fully parsed.",
            issues=issues,
            recommendations=recommendations,
            missing_topics=missing_topics,
            duplicates=duplicates,
            dependency_issues=dependency_issues,
            learning_objective_mapping=objective_mapping,
        )


def _score_from_issues(result: ValidationResult) -> None:
    blockers = _count_by_severity(result.issues, "blocker")
    warnings = _count_by_severity(result.issues, "warning")
    infos = _count_by_severity(result.issues, "info")

    if result.coverage_score <= 0:
        result.coverage_score = max(0.0, 100.0 - (18.0 * len(result.missing_topics)) - (6.0 * warnings))
    if result.sequence_score <= 0:
        sequence_penalty = 20.0 * len(result.dependency_issues)
        result.sequence_score = max(0.0, 100.0 - sequence_penalty - (5.0 * warnings))
    if result.relevance_score <= 0:
        relevance_penalty = 12.0 * len(result.duplicates) + (8.0 * blockers)
        result.relevance_score = max(0.0, 100.0 - relevance_penalty - (3.0 * warnings))
    if result.completeness_score <= 0:
        result.completeness_score = max(0.0, 100.0 - (12.0 * blockers) - (4.0 * warnings) - (1.0 * infos))
    if result.overall_score <= 0:
        result.overall_score = (
            0.30 * result.coverage_score
            + 0.25 * result.sequence_score
            + 0.25 * result.relevance_score
            + 0.20 * result.completeness_score
        )
    if result.confidence <= 0:
        confidence = 0.95 - (0.18 * blockers) - (0.04 * warnings)
        result.confidence = max(0.05, min(1.0, confidence))


def _finalize_result(result: ValidationResult) -> None:
    _score_from_issues(result)

    blockers = _count_by_severity(result.issues, "blocker")
    pass_criteria_met = (
        blockers == 0
        and result.overall_score >= 85.0
        and result.confidence >= 0.80
    )
    result.status = "PASS" if pass_criteria_met else "FAIL"
    result.retry_required = bool(result.retry_required or not pass_criteria_met)

    if result.retry_required and not result.retry_prompt:
        strongest_issue = next(
            (issue for issue in result.issues if issue.severity == "blocker"),
            result.issues[0] if result.issues else None,
        )
        issue_text = strongest_issue.message if strongest_issue else "Improve semantic alignment."
        missing = ", ".join(t.topic for t in result.missing_topics[:5] if t.topic)
        duplicate_text = ", ".join(result.duplicates[:5])
        result.retry_prompt = (
            "Regenerate the Topic Outline with strict adherence to user requirements and rule-pack constraints. "
            f"Primary issue: {issue_text}. "
            + (f"Missing topics to include: {missing}. " if missing else "")
            + (f"Remove or merge duplicates: {duplicate_text}. " if duplicate_text else "")
            + "Ensure prerequisite topics appear before advanced topics, and map each learning objective explicitly."
        ).strip()

    if result.status == "FAIL" and blockers == 0:
        result.issues.append(
            ValidationIssue(
                field="s1_ai_validator.pass_criteria",
                expected="overall_score >= 85 and confidence >= 0.8 with zero blockers",
                found={
                    "overall_score": round(result.overall_score, 2),
                    "confidence": round(result.confidence, 3),
                    "blockers": blockers,
                },
                severity="warning",
                message="AI pass criteria not met for the Topic Outline (non-blocking quality gate).",
                rule_source="s1_ai_validator.pass_criteria",
                failure_reason=(
                    "Outline quality score/confidence is below release threshold "
                    "even though no explicit blocker issue was produced."
                ),
                remediation="Regenerate TO using retry_prompt guidance and re-run S1 semantic validation.",
            )
        )


class SemanticValidator(BaseValidator):
    """Primary LLM-backed semantic validator."""

    name = "semantic"

    def run(
        self,
        *,
        payload: dict[str, Any],
        priority_rule: str,
    ) -> ValidationResult:
        _ = priority_rule
        config = LLMConfig(
            deployment=get_deployment("A0_TO"),
            max_tokens=4096,
            response_format={"type": "json_object"},
        )

        prompt = _build_system_prompt()
        started = time.perf_counter()
        payload_json = json.dumps(payload, ensure_ascii=False)
        logger.info(
            "[S1][AI] Starting semantic validation | payload_bytes=%d | sections=%d",
            len(payload_json.encode("utf-8")),
            payload.get("course_outline", {}).get("total_sections", 0),
        )

        raw_data = self._call_llm_with_retries(prompt, payload_json, config)
        result = _to_validation_result(raw_data)
        _finalize_result(result)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "[S1][AI] Validation complete | duration_ms=%d | status=%s | overall=%.1f | confidence=%.2f | retry=%s",
            elapsed_ms,
            result.status,
            result.overall_score,
            result.confidence,
            result.retry_required,
        )
        logger.info(
            "[S1][AI] Scores | coverage=%.1f | sequence=%.1f | relevance=%.1f | completeness=%.1f",
            result.coverage_score,
            result.sequence_score,
            result.relevance_score,
            result.completeness_score,
        )
        logger.info(
            "[S1][AI] Issues summary | blockers=%d | warnings=%d | infos=%d",
            _count_by_severity(result.issues, "blocker"),
            _count_by_severity(result.issues, "warning"),
            _count_by_severity(result.issues, "info"),
        )
        return result

    def _call_llm_with_retries(self, system_prompt: str, payload_json: str, config: LLMConfig) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, MAX_LLM_RETRIES + 2):
            try:
                raw = llm_chat(system_prompt, payload_json, config, agent="S1")
                data = _safe_json_loads(raw)
                if not isinstance(data, dict):
                    raise ValueError(f"Expected JSON object, got {type(data).__name__}")
                return data
            except Exception as exc:
                last_error = exc
                if attempt > MAX_LLM_RETRIES:
                    break
                delay = RETRY_BACKOFF_SECONDS * attempt
                logger.warning(
                    "[S1][AI] LLM attempt %d failed (%s). Retrying in %.2fs...",
                    attempt,
                    exc,
                    delay,
                )
                time.sleep(delay)
        assert last_error is not None
        raise last_error


__all__ = ["SemanticValidator", "_finalize_result"]
