"""
S2 validation checks — aggregator module.

All checks are split across three focused sub-modules:
  • kc_checks.py          — Knowledge-check structure, distractor rationales, placement
  • content_checks.py     — A2 completeness, compliance, voice/tone, structure, LO coverage
  • word_count_checks.py  — Lectora limits, TO target deviation, course bands, doc-bounds

This file re-exports every public check so that existing imports continue to work
without any changes to callers.
"""

from .kc_checks import (
    check_kc_distractor_rationales,
    check_kc_placement,
    check_kc_structure,
)
from .content_checks import (
    check_a2_completeness,
    check_callouts_per_section,
    check_examples_per_section,
    check_forbidden_phrases,
    check_intro_section,
    check_lo_coverage,
    check_los_in_first_section,
    check_no_duplicate_headings,
    check_regulatory_mode,
    check_required_behaviors,
    check_section_non_empty,
    check_summary_section,
    check_voice_pronouns,
)
from .word_count_checks import (
    check_course_word_count_bands,
    check_lectora_page_limits,
    check_word_count_against_doc_bounds,
    check_word_count_target,
)

__all__ = [
    # KC checks
    "check_kc_distractor_rationales",
    "check_kc_placement",
    "check_kc_structure",
    # Content checks
    "check_a2_completeness",
    "check_callouts_per_section",
    "check_examples_per_section",
    "check_forbidden_phrases",
    "check_intro_section",
    "check_lo_coverage",
    "check_los_in_first_section",
    "check_no_duplicate_headings",
    "check_regulatory_mode",
    "check_required_behaviors",
    "check_section_non_empty",
    "check_summary_section",
    "check_voice_pronouns",
    # Word-count / pacing checks
    "check_course_word_count_bands",
    "check_lectora_page_limits",
    "check_word_count_against_doc_bounds",
    "check_word_count_target",
]
