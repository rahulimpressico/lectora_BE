"""
Insurance CE — per-level overlays (basic, intermediate, advanced).

Merged onto the base ``insurance_ce.PACK`` by ``resolve_rule_pack(family, difficulty)``.
"""

from __future__ import annotations

DIFFICULTY_LEVELS: dict[str, dict] = {
    "basic": {
        "style_constraints": {
            "difficulty_level": "basic",
            "reading_level": "Max 8th grade — plain language only",
            "tone": "simple_conversational_beginner",
            "max_sentences_per_paragraph": 4,
            "avoid_complex_jargon": True,
            "explain_terms_on_first_use": True,
            "require_scenario_based_examples": True,
        },
        "content_rules": {
            "require_examples_per_section": [2, 4],
            "require_callouts_per_section": [1, 2],
            # 180 wpm × 50 min/hr = 9,000 words/CE hr × 1.0 = 9,000
            "difficulty_multiplier": 1.0,
            "words_per_credit_hour": 9000,
        },
        "compliance_elements": {
            "required_behaviors": [
                "use very short sentences and everyday words — assume no prior insurance background",
                "define every technical term on first use with a one-line plain-English gloss",
                "use extra lightweight examples so concepts feel approachable",
            ],
        },
        "kc_placement_rules": {
            "min_kc_per_lesson": 1,
            "max_kc_per_lesson": 6,
            "distractor_quality": "clearly_differentiated",
        },
        "lectora_constraints": {
            "max_words_per_page": 350,
        },
    },
    "intermediate": {
        "style_constraints": {
            "difficulty_level": "intermediate",
            "reading_level": "Max 9th grade",
            "tone": "conversational_professional_beginner_friendly",
            "max_sentences_per_paragraph": 5,
            "avoid_complex_jargon": True,
            "explain_terms_on_first_use": True,
            "require_scenario_based_examples": True,
            "require_transition_sentences": True,
        },
        "content_rules": {
            "require_examples_per_section": [1, 3],
            "require_callouts_per_section": [1, 3],
            # 9,000 × 1.25 = 11,250 words/CE hr
            "difficulty_multiplier": 1.25,
            "words_per_credit_hour": 11250,
        },
        "compliance_elements": {
            "required_behaviors": [
                "balance clarity with professional insurance practice — student has some CE background",
                "use lightweight scenarios that mirror typical agent/adjuster situations",
            ],
        },
        "kc_placement_rules": {
            "min_kc_per_lesson": 2,
            "max_kc_per_lesson": 8,
            "distractor_quality": "plausible",
        },
        "lectora_constraints": {
            "max_words_per_page": 400,
        },
    },
    "advanced": {
        "style_constraints": {
            "difficulty_level": "advanced",
            "reading_level": "Grade 10–12 — professional CE depth",
            "tone": "professional_analytical",
            "max_sentences_per_paragraph": 6,
            "avoid_complex_jargon": False,
            "explain_terms_on_first_use": True,
            "require_scenario_based_examples": True,
            "require_transition_sentences": True,
        },
        "content_rules": {
            "require_examples_per_section": [1, 2],
            "require_callouts_per_section": [1, 2],
            # 9,000 × 1.5 = 13,500 words/CE hr
            "difficulty_multiplier": 1.5,
            "words_per_credit_hour": 13500,
        },
        "compliance_elements": {
            "required_behaviors": [
                "assume an experienced insurance professional — go deeper on nuance and edge cases",
                "use application- and analysis-level scenarios (not just definitions)",
                "connect concepts to regulatory judgment and suitability where the source supports it",
            ],
        },
        "assessment_rules": {
            "require_distractor_rationales": True,
        },
        "kc_placement_rules": {
            "min_kc_per_lesson": 2,
            "max_kc_per_lesson": 10,
            "distractor_quality": "plausible_subtle",
            "kc_triggers": [
                "important_new_concepts",
                "complex_or_difficult_explanations",
                "section_or_subsection_completion",
                "scenario_or_case_study_interactions",
                "application_and_analysis",
            ],
        },
        "lectora_constraints": {
            "max_words_per_page": 400,
        },
    },
}
