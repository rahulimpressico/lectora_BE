from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_LLM_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.75


class ValidationIssue(BaseModel):
    field: str = Field(default="s1_ai_validator")
    expected: str = Field(default="Requirement satisfied")
    found: Any = Field(default="Not satisfied")
    severity: Literal["blocker", "warning", "info"] = "warning"
    message: str = Field(default="AI validator found an outline issue.")
    rule_source: str = Field(default="s1_ai_validator")
    failure_reason: str | None = None
    remediation: str | None = None


class Recommendation(BaseModel):
    title: str = Field(default="")
    detail: str = Field(default="")
    priority: Literal["high", "medium", "low"] = "medium"


class MissingTopic(BaseModel):
    topic: str = Field(default="")
    reason: str = Field(default="")
    severity: Literal["high", "medium", "low"] = "medium"


class DependencyIssue(BaseModel):
    topic: str = Field(default="")
    missing_prerequisite: str = Field(default="")
    reason: str = Field(default="")


class ObjectiveMapping(BaseModel):
    objective: str = Field(default="")
    status: Literal["covered", "partial", "missing"] = "partial"
    evidence: str = Field(default="")


class ValidationResult(BaseModel):
    summary: str = ""
    overall_score: float = 0.0
    coverage_score: float = 0.0
    sequence_score: float = 0.0
    relevance_score: float = 0.0
    completeness_score: float = 0.0
    confidence: float = 0.0
    status: Literal["PASS", "FAIL"] = "FAIL"
    issues: list[ValidationIssue] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    missing_topics: list[MissingTopic] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)
    dependency_issues: list[DependencyIssue] = Field(default_factory=list)
    learning_objective_mapping: list[ObjectiveMapping] = Field(default_factory=list)
    retry_required: bool = False
    retry_prompt: str = ""


class BaseValidator(ABC):
    """Base extension point for future S1 AI validators."""

    name: str = "base"

    @abstractmethod
    def run(
        self,
        *,
        payload: dict[str, Any],
        priority_rule: str,
    ) -> ValidationResult:
        raise NotImplementedError


class CoverageValidator(BaseValidator):
    """Post-processing extension point for future non-LLM coverage checks."""

    name = "coverage"

    def run(self, *, payload: dict[str, Any], priority_rule: str) -> ValidationResult:  # pragma: no cover - placeholder
        _ = payload
        _ = priority_rule
        return ValidationResult(
            summary="Coverage validator placeholder.",
            status="PASS",
        )


class SequenceValidator(BaseValidator):
    """Post-processing extension point for future non-LLM sequence checks."""

    name = "sequence"

    def run(self, *, payload: dict[str, Any], priority_rule: str) -> ValidationResult:  # pragma: no cover - placeholder
        _ = payload
        _ = priority_rule
        return ValidationResult(
            summary="Sequence validator placeholder.",
            status="PASS",
        )


__all__ = [
    "MAX_LLM_RETRIES",
    "RETRY_BACKOFF_SECONDS",
    "BaseValidator",
    "CoverageValidator",
    "DependencyIssue",
    "MissingTopic",
    "ObjectiveMapping",
    "Recommendation",
    "SequenceValidator",
    "ValidationIssue",
    "ValidationResult",
]
