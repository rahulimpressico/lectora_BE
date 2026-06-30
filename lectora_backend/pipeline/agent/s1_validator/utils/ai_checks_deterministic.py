"""Compatibility wrapper for legacy ai_checks_deterministic imports."""

from .ai.deterministic import _check_required_topics_deterministic, _required_topics_to_issues

__all__ = ["_check_required_topics_deterministic", "_required_topics_to_issues"]

