"""
IARCE (E&PR — Ethics & Professional Responsibility, NASAA / SEC) rule pack.
"""

from __future__ import annotations

PACK: dict = {
    "id": "rp-iarce-v3.6",
    "family": "IARCE",
    "version": "3.6",
    "full_name": (
        "Investment Adviser Representative Continuing Education - "
        "Ethics & Professional Responsibility"
    ),
    "governed_by": "NASAA / SEC",
    "audience": "IARs exclusively",
    "exam_file_format_samples": "DOCX",
    "unique_artifacts": ["Timed Outline", "Ethics Category Application"],
    "sample_courses_available": ["IAR_3509 Ethics of Behavioral Financial Planning"],
    "new_course_requested": "Best Execution for IARs (1 credit E&PR)",
    # ~50% of IARs dually registered; FINRA, state, SEC may apply; course framing SEC-dominant
    "dually_registered_iar_context": {
        "estimated_dually_registered_share": 0.5,
        "jurisdictions_relevant": ["finra", "state", "sec"],
        "course_regulatory_emphasis": "sec_dominant",
    },
    "assessment_rules": {
        "final_exam_min_questions": 20,
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
        # Question distribution: fairly distributed across course content
        "objective_coverage_required": True,
        # Cross-reference required: No
        "require_exam_cross_reference": False,
    },
    "style_constraints": {
        "reading_level": "Clean and explanatory; may trend higher than Firm Element",
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
            "refer to learners in third person using role titles (e.g. IAR)",
            "use 'this course' for organizational reference; do not use 'we'",
            # First-mention formatting
            "bold the first mention of a regulatory body (full name + acronym)",
            "bold the first mention of a rule or regulation",
            # Sources
            "cite only primary regulatory sources (SEC, FINRA, MSRB, NASAA, CFTC, NFA, FinCEN, FATF, CFPB, FRB, OCC, IRS)",
            "do not cite law blogs or consulting/marketing websites",
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
        # Opening structure and learning objectives count
        "require_intro_section": True,
        "require_learning_objectives": True,
        # Course: 5–10 active-verb learning goals (timed outline Table 3); S1 validates total count
        "learning_objectives_range": [5, 10],
        "require_active_verb_learning_objectives": True,
        # Each lesson: 3–5 learning objectives
        "learning_objectives_per_lesson_range": [3, 5],
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
        # Timed outline required: Yes
        "require_timed_outline": True,
        # Ethics category application required: Yes
        "require_ethics_category_application": True,
        # Credit/hour structure
        "words_per_credit_hour": 9000,
        "course_word_count_bands": None,
        # Baseline quality constraints
        "no_duplicate_concepts_across_sections": True,
        "no_unverified_statistics": True,
        "no_opinion_based_statements": True,
        "self_contained_subtopics": True,
        "maintain_section_boundary_integrity": True,
    },
    "kc_placement_rules": {
        "placement": "content_driven_not_positionally_predictable",
        "min_kc_per_lesson": 2,
        "max_kc_per_lesson": 5,
        # KC answer options: 4 only
        "min_answer_options": 4,
        "max_answer_options": 4,
        "cadence": {
            "screens_min": 3,
            "screens_max": 6,
            "approximate_word_pages_min": 1,
            "approximate_word_pages_max": 3,
        },
        "kc_triggers": [
            "important_new_concepts",
            "complex_or_difficult_explanations",
            "section_or_subsection_completion",
            "scenario_or_case_study_interactions",
        ],
        "embedded_kc_format": {
            "typical_answer_option_count": 4,
            "components": [
                "stem",
                "answer_options",
                "correct_answer",
                "explanation",
            ],
            "allow_scenario_or_case_study": True,
            "prefer_scenario_or_case_study": True,
        },
        "forbidden_placements": [
            "mid_paragraph",
            "inside_regulatory_block",
        ],
        "require_explanation": True,
        "require_explanation_for_all_options": True,
        "distractor_quality": "plausible",
        "prefer_scenario_based_stems": True,
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
