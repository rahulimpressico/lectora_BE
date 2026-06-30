"""Compatibility facade for AI outline checks."""

from .ai import (
    MAX_LLM_RETRIES,
    RETRY_BACKOFF_SECONDS,
    BaseValidator,
    CoverageValidator,
    DependencyIssue,
    MissingTopic,
    ObjectiveMapping,
    Recommendation,
    SemanticValidator,
    SequenceValidator,
    ValidationIssue,
    ValidationResult,
    _check_required_topics_deterministic,
    _finalize_result,
    _required_topics_to_issues,
    run_ai_outline_checks,
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
    "SemanticValidator",
    "SequenceValidator",
    "ValidationIssue",
    "ValidationResult",
    "_check_required_topics_deterministic",
    "_required_topics_to_issues",
    "_finalize_result",
    "run_ai_outline_checks",
]
