"""Compatibility wrapper for legacy a1_checks imports."""

from .checks_domain.a1 import (
    check_a1_assessment_rules,
    check_a1_credit_hours,
    check_a1_credit_hours_against_rule_pack,
    check_a1_kc_count,
    check_a1_learning_objectives_range,
    check_a1_lo_coverage,
    check_a1_sections,
    check_a1_word_counts,
)

__all__ = [
    "check_a1_sections",
    "check_a1_word_counts",
    "check_a1_kc_count",
    "check_a1_lo_coverage",
    "check_a1_learning_objectives_range",
    "check_a1_credit_hours_against_rule_pack",
    "check_a1_credit_hours",
    "check_a1_assessment_rules",
]

