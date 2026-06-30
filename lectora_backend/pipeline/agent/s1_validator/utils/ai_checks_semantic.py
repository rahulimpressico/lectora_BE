"""Compatibility wrapper for legacy ai_checks_semantic imports."""

from .ai.semantic import SemanticValidator, _finalize_result

__all__ = ["SemanticValidator", "_finalize_result"]

