"""
Firm Element (FINRA Rule 1240 — Firm Element Continuing Education) rule pack.
"""

from __future__ import annotations

PACK: dict = {
    "id": "rp-firm-element-v2.4",
    "family": "Firm Element",
    "version": "2.4",
    "full_name": "Firm Element Continuing Education",
    "governed_by": "FINRA Rule 1240",
    "audience": (
        "Broker-Dealer reps (RRs)/IARs, investment professionals, Supervisors/Principals, "
        "Branch Managers, Desk Supervisors, Compliance/Risk, Operations & back office, C-level, "
        "Sales support & client-facing non-registered staff"
    ),
    "exam_file_format_samples": "DOCX",
    "sample_courses_available": [
        "932 Senior Safe Act",
        "959 Due Diligence New Complex and Private Offerings",
    ],
    "new_course_requested": None,
    "unique_artifacts": [],
    "assessment_rules": {
        "final_exam_min_questions": 15,
        "answer_options_count": 4,
        "allow_true_false": False,
        "allow_all_of_the_above": False,
        "forbidden_question_types": [
            "true_false",
            "all_of_the_above",
            "none_of_the_above",
            "except_questions",
            "roman_numeral_questions",
        ],
        # Exam feedback (RESPONSE) required: for every answer choice correct and incorrect
        "require_rationale": True,
        "require_distractor_rationales": True,
        "objective_coverage_required": True,
        # Cross-reference required: No
        "require_exam_cross_reference": False,
    },
    "style_constraints": {
        # Reading level: Max 9th grade; translate complex ideas into explanations
        "reading_level": "Grade 9 maximum; plain language; translate complex ideas into clear explanations",
        # Style: Formal and direct clean prose
        "voice": "third_person_role_title",
        "tone": "formal_direct_clean",
        "paragraph_length": "short",
        "max_sentences_per_paragraph": 5,
        "avoid_complex_jargon": True,
        "explain_terms_on_first_use": True,
        "bold_first_key_term": True,
    },
    "compliance_elements": {
        "regulatory_mode": "strict_real_regulators",
        "require_non_advisory_language": False,
        "forbidden_phrases": [],
        "required_behaviors": [
            # Learner/org references
            "refer to learners in third person using role titles (e.g. registered representative)",
            "use 'this course' for organizational reference; do not use 'we'",
            # First-mention formatting
            "bold the first mention of a regulatory body (full name + acronym)",
            "bold the first mention of a rule or regulation",
            # Sources
            "cite only primary regulatory sources (SEC, FINRA, MSRB, NASAA, CFTC, NFA, FinCEN, FATF, CFPB, FRB, OCC, IRS)",
            "do not cite law blogs or consulting/marketing websites",
            # General tone safety
            "use neutral explanations",
            "avoid financial advice tone",
            "frame statements as informational",
            "avoid unsupported claims",
        ],
        "disclosure_handling": {
            "allow_generic_regulatory_reference": False,
            "no_hallucinated_citations": False,
        },
    },
    "content_rules": {
        "must_map_to_learning_objectives": True,
        "require_learning_objectives_in_first_section": None,
        "require_expanded_summary_section": None,
        # Opening structure and LO count
        "require_intro_section": True,
        "require_learning_objectives": True,
        "learning_objectives_range": [5, 10],
        # Content starts at section 2.0; hierarchy levels up to 4
        # (represented in A1 parsing + doc structure expectations, kept as guidance elsewhere)
        # For example sections / callouts
        "require_examples_per_section": [1, 2],
        "require_callouts_per_section": [1, 2],
        # Case studies: optional; fictionalized narrative/dialogue; KCs may advance narrative
        "allow_case_studies": True,
        "case_study_policy": {
            "optional": True,
            "allow_fictionalized_narrative_or_dialogue": True,
            "knowledge_checks_advance_narrative": True,
        },
        "allow_regulatory_updates_section": True,
        "require_timed_outline": False,
        "require_ethics_category_application": None,
        # Credit/hour structure
        "words_per_credit_hour": 6000,
        "course_word_count_bands": {"short": 3000, "typical": 6000, "long": 28000},
        # Baseline quality constraints
        "no_duplicate_concepts_across_sections": True,
        "no_unverified_statistics": True,
        "no_opinion_based_statements": True,
        "self_contained_subtopics": True,
        "maintain_section_boundary_integrity": True,
    },
    "kc_placement_rules": {
        # KC placement rule: content-driven, not positionally predictable
        "placement": "content_driven_not_positionally_predictable",
        "min_kc_per_lesson": 1,
        "max_kc_per_lesson": 4,
        # KC answer options: 4 only
        "min_answer_options": 4,
        "max_answer_options": 4,
        "forbidden_placements": [
            "mid_paragraph",
            "after_table",
            "inside_regulatory_block",
        ],
        # KC structure: stem + options + correct answer with explanation
        "require_explanation": True,
        "distractor_quality": "plausible",
    },
    "deduplication_rules": {
        "similarity_threshold": 0.82,
        "apply_between": [
            "KC_to_KC",
            "KC_to_Exam",
            "Exam_to_Exam",
        ],
    },
    "lectora_constraints": {
        "max_words_per_page": 180,
        "prefer_bulleted_content": True,
        "allow_callouts": True,
        "allow_tables": True,
        "avoid_large_text_blocks": True,
        "page_break_strategy": "subtopic_based",
    },
    "error_tolerance": {
        "word_count_tolerance_percent": 10,
        "retry_on_failure": True,
        "max_retries_per_step": 3,
    },
}
