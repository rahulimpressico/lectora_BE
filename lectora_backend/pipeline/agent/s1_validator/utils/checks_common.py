"""Compatibility wrapper for legacy checks_common imports."""

from .checks_domain.common import (
    credit_hours_derived,
    credit_hours_from_rule_pack,
    difficulty_multiplier,
    kc_count_from_sections,
    round_credit_hours,
    total_words_from_sections,
)

__all__ = [
    "total_words_from_sections",
    "kc_count_from_sections",
    "round_credit_hours",
    "difficulty_multiplier",
    "credit_hours_derived",
    "credit_hours_from_rule_pack",
]

