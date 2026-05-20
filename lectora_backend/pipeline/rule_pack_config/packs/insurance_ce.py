"""
Insurance CE — state-regulated continuing education rule pack.

Difficulty: ``resolve_rule_pack("insurance_ce", "basic"|"intermediate"|"advanced")``
merges overlays from ``insurance_ce_difficulty.DIFFICULTY_LEVELS``.
"""

from __future__ import annotations

from .insurance_ce_difficulty import DIFFICULTY_LEVELS

PACK: dict = {
    "id": "rp-insurance-ce-v3.4",
    "family": "Insurance CE",
    "version": "3.4",
    "default_difficulty": "intermediate",
    "difficulty_levels": DIFFICULTY_LEVELS,
    # Metadata per provided table (not used for enforcement directly)
    "full_name": "Insurance Continuing Education",
    "governed_by": "State Insurance Regulators",
    "audience": "Insurance agents, adjusters, back-office support staff",
    "exam_file_format_samples": "Raw TXT file",
    "sample_courses_available": [
        "533 Flood Insurance",
        "537 Washington",
        "239_8 Montana",
        "534 Louisiana",
        "401_2 Unknown",
    ],
    "new_course_requested": "Employer-Provided Health Insurance Plans (3 hrs ~27000 words)",
    "unique_artifacts": [],
    "assessment_rules": {
        # Final exam questions: 75 standard (fewer for single-state / unusually short courses)
        "final_exam_min_questions": 75,
        "answer_options_count": 4,
        # True/False allowed in KCs, but prohibited on final exam
        "allow_true_false": False,
        "allow_all_of_the_above": False,
        "forbidden_question_types": [
            "true_false",
            "all_of_the_above",
            "none_of_the_above",
            "roman_numeral_questions",
        ],
        # Exam feedback required: explain every choice (correct + incorrect)
        "require_rationale": True,
        "require_distractor_rationales": True,
        # Question distribution: every section covered except intros and summaries
        "objective_coverage_required": True,
        # Cross-reference required: section number + page number (primary source if multiple)
        "require_exam_cross_reference": True,
    },
    "style_constraints": {
        "reading_level": "Max 9th grade",
        # Learner: "you", Organization: "we", Clients/claimants: "they"
        "voice": "second_person_you_organization_we_clients_they",
        "tone": "conversational_professional_beginner_friendly",
        "teaching_style": "human_mentor_not_robotic",
        "paragraph_length": "short",
        "max_sentences_per_paragraph": 5,
        "avoid_complex_jargon": True,
        "explain_terms_on_first_use": True,
        "bold_first_key_term": True,
        "require_scenario_based_examples": True,
        "require_transition_sentences": True,
        "instructional_emphasis_labels": [
            "Important",
            "Pro Tip",
            "Common Mistake",
            "Warning",
            "Best Practice",
        ],
        "audience_focus": "students",
    },
    "compliance_elements": {
        "regulatory_mode": "strict_real_regulators",
        "require_non_advisory_language": False,
        "forbidden_phrases": [],
        "required_behaviors": [
            # References
            "address learners with second-person 'you'",
            "use 'we' for organization reference",
            "refer to clients and claimants as 'they'",
            # Teaching style
            "write like a real mentor — practical, conversational, immersive; avoid stiff AI tone",
            "include lightweight scenario-based explanations and real-world examples in each section",
            "bridge topics with smooth transition sentences for natural flow",
            "use labeled instructional callouts (Important, Pro Tip, Common Mistake, Warning, Best Practice)",
            "include lightweight scenario-based examples throughout each section",
            "ground teaching points in the provided source excerpt — paraphrase faithfully; do not invent unsupported facts",
            # Sources
            "anchor regulatory references to state insurance regulators (e.g., state departments of insurance)",
        ],
        "disclosure_handling": {
            "allow_generic_regulatory_reference": False,
            "no_hallucinated_citations": False,
        },
    },
    "content_rules": {
        "must_map_to_learning_objectives": True,
        # Opening: learning objectives in first section
        "require_learning_objectives_in_first_section": True,
        # End section: summary recapping objectives (slightly expanded with key details)
        "require_expanded_summary_section": True,
        "require_conclusion_section": True,
        "require_source_fidelity": True,
        "require_intro_section": None,
        "require_learning_objectives": None,
        "learning_objectives_range": None,
        "require_examples_per_section": [1, 3],
        "require_callouts_per_section": [1, 3],
        # Aligns with kc_placement_rules.embedded_kc_format.allow_scenario_or_case_study
        "allow_case_studies": True,
        "allow_regulatory_updates_section": None,
        "require_timed_outline": False,
        "require_ethics_category_application": None,
        "words_per_credit_hour": 9000,
        # Difficulty scaling: basic 1.0×, intermediate 1.25×, advanced 1.5×
        # Overridden per-difficulty in insurance_ce_difficulty.py
        "difficulty_multiplier": 1.0,
        "course_word_count_bands": None,
        "no_duplicate_concepts_across_sections": True,
        "no_unverified_statistics": True,
        "no_opinion_based_statements": True,
        "self_contained_subtopics": True,
        "maintain_section_boundary_integrity": True,
    },
    "kc_placement_rules": {
        "placement": "every_5_to_10_screens_instructional_priority",
        "min_kc_per_lesson": 2,
        "max_kc_per_lesson": 8,
        "min_answer_options": 2,
        "max_answer_options": 4,
        "cadence": {
            "screens_min": 5,
            "screens_max": 10,
            "approximate_word_pages_min": 2,
            "approximate_word_pages_max": 4,
        },
        "placement_priorities": [
            "instructional_value",
            "after_important_or_complex_concepts",
            "end_of_section_or_subsection",
            "after_scenarios",
        ],
        "interrupt_policy": {
            "avoid_unnecessary_interruption": True,
            "allow_interrupt_long_explanations_for_frequency": True,
        },
        "avoid_kc_on": [
            "inflation_adjusted_figures",
            "predictably_changing_items",
        ],
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
        },
        "forbidden_placements": [
            "introduction",
            "opening_section",
            "summary_section",
            "course_summary",
            "mid_paragraph",
            "after_table",
            "inside_regulatory_block",
        ],
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
