"""Compatibility wrapper for legacy a0_checks imports."""

from .checks_domain.a0 import (
    check_a0_classification,
    check_a0_images,
    check_a0_metadata,
    check_a0_timed_outline_required,
)

__all__ = [
    "check_a0_metadata",
    "check_a0_classification",
    "check_a0_timed_outline_required",
    "check_a0_images",
]

