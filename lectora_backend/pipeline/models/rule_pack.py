"""
Rule Pack Pydantic models.

Mirrors the structure of RULE_PACKS in rule_pack_config/rule_packs.py.
These models are the single source of schema truth for validation, serialisation,
and IDE autocompletion across all agents.
"""

from typing import Annotated, Any, Optional

from pydantic import BaseModel, Field, model_validator


# ── Assessment Rules ──────────────────────────────────────────────────────────

class QuestionFormatDistribution(BaseModel):
    """Fractional split of final-exam question formats (must sum ≤ 1.0)."""

    scenario_based: float = Field(ge=0.0, le=1.0)
    definition_based: float = Field(ge=0.0, le=1.0)
    conceptual: float = Field(ge=0.0, le=1.0)

    model_config = {
        "json_schema_extra": {
            "examples": [{"scenario_based": 0.4, "definition_based": 0.3, "conceptual": 0.3}]
        }
    }


class AssessmentRules(BaseModel):
    """Hard constraints that govern how questions are written and validated."""

    final_exam_min_questions: int = Field(gt=0)
    answer_options_count: int = Field(gt=1, description="Number of answer choices per MCQ.")
    allow_true_false: bool
    allow_all_of_the_above: bool
    forbidden_question_types: list[str] = Field(
        default_factory=list,
        description="Question types that must never appear.",
    )
    question_format_distribution: Optional[QuestionFormatDistribution] = Field(
        default=None,
        description=(
            "Optional fractional split of exam question formats. "
            "If not specified by the rule pack, omit and let the generator choose."
        ),
    )
    require_rationale: bool = Field(description="Whether each answer must include an explanation.")
    require_distractor_rationales: bool = Field(
        default=True,
        description="If True, explanations must also address why each incorrect option is wrong.",
    )
    objective_coverage_required: bool = Field(description="All LOs must be addressed by at least one question.")
    require_exam_cross_reference: Optional[bool] = Field(
        None,
        description="If True, final exam items must cite section number and page (primary source when multiple).",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "final_exam_min_questions": 15,
                    "answer_options_count": 4,
                    "allow_true_false": False,
                    "allow_all_of_the_above": False,
                    "forbidden_question_types": ["true_false", "all_of_the_above"],
                    "question_format_distribution": {
                        "scenario_based": 0.4,
                        "definition_based": 0.3,
                        "conceptual": 0.3,
                    },
                    "require_rationale": True,
                    "objective_coverage_required": True,
                }
            ]
        }
    }


# ── Style Constraints ─────────────────────────────────────────────────────────

class StyleConstraints(BaseModel):
    """Writing-style guidelines enforced during content generation (A2)."""

    reading_level: str = Field(min_length=1)
    voice: str = Field(min_length=1, description="E.g. 'second_person', 'third_person_professional'.")
    tone: str = Field(min_length=1)
    paragraph_length: str = Field(min_length=1, description="E.g. 'short', 'medium'.")
    max_sentences_per_paragraph: int = Field(gt=0)
    avoid_complex_jargon: bool
    explain_terms_on_first_use: bool
    bold_first_key_term: bool
    teaching_style: Optional[str] = Field(
        None,
        description="E.g. human_mentor_not_robotic — conversational mentor voice.",
    )
    require_scenario_based_examples: Optional[bool] = Field(
        None,
        description="When true, each section should include scenario or real-world examples.",
    )
    require_transition_sentences: Optional[bool] = Field(
        None,
        description="When true, use bridging sentences between major ideas within a section.",
    )
    instructional_emphasis_labels: Optional[list[str]] = Field(
        None,
        description="Allowed callout labels for important_callout blocks (e.g. Pro Tip, Warning).",
    )
    audience_focus: Optional[str] = Field(
        None,
        description="Primary reader, e.g. students — drives mentor/student tone in A2.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "reading_level": "Grade 10-12",
                    "voice": "second_person",
                    "tone": "neutral_instructional_compliance",
                    "paragraph_length": "short",
                    "max_sentences_per_paragraph": 5,
                    "avoid_complex_jargon": True,
                    "explain_terms_on_first_use": True,
                    "bold_first_key_term": True,
                }
            ]
        }
    }


# ── Compliance Elements ───────────────────────────────────────────────────────

class DisclosureHandling(BaseModel):
    allow_generic_regulatory_reference: bool
    no_hallucinated_citations: bool

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"allow_generic_regulatory_reference": True, "no_hallucinated_citations": True}
            ]
        }
    }


class ComplianceElements(BaseModel):
    """Regulatory compliance guardrails that apply during A2 content generation."""

    regulatory_mode: str = Field(
        min_length=1,
        description="One of 'safe_placeholder' or 'strict_real_regulators'.",
    )
    require_non_advisory_language: bool
    forbidden_phrases: list[str] = Field(description="Phrases that must never appear in generated text.")
    required_behaviors: list[str] = Field(description="Authoring behaviours the LLM must follow.")
    disclosure_handling: DisclosureHandling

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "regulatory_mode": "safe_placeholder",
                    "require_non_advisory_language": True,
                    "forbidden_phrases": ["you should invest", "best option"],
                    "required_behaviors": ["use neutral explanations", "avoid financial advice tone"],
                    "disclosure_handling": {
                        "allow_generic_regulatory_reference": True,
                        "no_hallucinated_citations": True,
                    },
                }
            ]
        }
    }


# ── Content Rules ─────────────────────────────────────────────────────────────


class CaseStudyPolicy(BaseModel):
    """When case studies are allowed, how they may be written and how KCs may interact."""

    optional: bool = Field(
        True,
        description="If True, case studies are optional (not required in every course or section).",
    )
    allow_fictionalized_narrative_or_dialogue: bool = Field(
        True,
        description="May use fictionalized narrative and/or dialogue format.",
    )
    knowledge_checks_advance_narrative: bool = Field(
        True,
        description="Embedded knowledge checks may be used to advance the narrative.",
    )


class ContentRules(BaseModel):
    """Structural and pedagogical rules for course content."""

    must_map_to_learning_objectives: bool
    no_duplicate_concepts_across_sections: bool
    no_unverified_statistics: bool
    no_opinion_based_statements: bool
    self_contained_subtopics: bool
    maintain_section_boundary_integrity: bool

    # Pacing / credit-hour calibration (optional)
    words_per_credit_hour: Optional[float] = Field(
        None,
        gt=0,
        description="Expected words per approved credit hour for this family (e.g. 9000).",
    )
    course_word_count_bands: Optional[dict[str, int]] = Field(
        None,
        description="Optional word-count bands for the full course (e.g. {'short': 3000, 'typical': 6000, 'long': 28000}).",
    )

    # IARCE-specific optional fields
    require_intro_section: Optional[bool] = None
    require_learning_objectives: Optional[bool] = None
    learning_objectives_range: Optional[Annotated[list[int], Field(min_length=2, max_length=2)]] = Field(
        None,
        description="[min, max] total learning objectives for the course (IARCE / Firm Element).",
    )
    learning_objectives_per_lesson_range: Optional[Annotated[list[int], Field(min_length=2, max_length=2)]] = Field(
        None,
        description="[min, max] learning objectives per lesson/section in generated content (IARCE).",
    )
    require_active_verb_learning_objectives: Optional[bool] = Field(
        None,
        description="If True, course and lesson objectives must use measurable active verbs (IARCE).",
    )
    require_examples_per_section: Optional[Annotated[list[int], Field(min_length=2, max_length=2)]] = Field(
        None,
        description="[min, max] examples required per section (IARCE only).",
    )
    require_callouts_per_section: Optional[Annotated[list[int], Field(min_length=2, max_length=2)]] = Field(
        None,
        description="[min, max] callout blocks required per section (IARCE only).",
    )
    allow_case_studies: Optional[bool] = None
    case_study_policy: Optional[CaseStudyPolicy] = Field(
        None,
        description="Structured case-study rules when allow_case_studies is true (e.g. Firm Element).",
    )
    allow_regulatory_updates_section: Optional[bool] = None
    require_timed_outline: Optional[bool] = Field(
        None,
        description="Deliverable must include a timed outline (IARCE E&PR).",
    )
    require_ethics_category_application: Optional[bool] = Field(
        None,
        description="Deliverable must include an ethics category application (IARCE E&PR).",
    )
    require_learning_objectives_in_first_section: Optional[bool] = Field(
        None,
        description="Learning objectives appear in the first / opening section (Insurance CE).",
    )
    require_expanded_summary_section: Optional[bool] = Field(
        None,
        description="Closing section recaps objectives with key details expanded (Insurance CE).",
    )
    require_conclusion_section: Optional[bool] = Field(
        None,
        description="When true, DOCX must include a final Conclusion section (A2 LLM).",
    )
    require_source_fidelity: Optional[bool] = Field(
        None,
        description="When true, section content must stay faithful to provided source excerpts.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "must_map_to_learning_objectives": True,
                    "no_duplicate_concepts_across_sections": True,
                    "no_unverified_statistics": True,
                    "no_opinion_based_statements": True,
                    "self_contained_subtopics": True,
                    "maintain_section_boundary_integrity": True,
                }
            ]
        }
    }


# ── KC Placement Rules ────────────────────────────────────────────────────────


class KcCadence(BaseModel):
    """Approximate spacing for embedded knowledge checks (screens / pages)."""

    screens_min: int = Field(ge=1, description="Minimum screens between KCs (inclusive).")
    screens_max: int = Field(ge=1, description="Maximum screens between KCs (inclusive).")
    approximate_word_pages_min: Optional[int] = Field(
        None,
        ge=1,
        description="Lower bound of typical Word pages per cadence window (optional).",
    )
    approximate_word_pages_max: Optional[int] = Field(
        None,
        ge=1,
        description="Upper bound of typical Word pages per cadence window (optional).",
    )

    @model_validator(mode="after")
    def _ordered_ranges(self) -> "KcCadence":
        if self.screens_max < self.screens_min:
            raise ValueError("screens_max must be >= screens_min")
        if (
            self.approximate_word_pages_min is not None
            and self.approximate_word_pages_max is not None
            and self.approximate_word_pages_max < self.approximate_word_pages_min
        ):
            raise ValueError("approximate_word_pages_max must be >= approximate_word_pages_min")
        return self


class KcInterruptPolicy(BaseModel):
    """When embedded KCs may break narrative flow."""

    avoid_unnecessary_interruption: bool = Field(
        description="Prefer natural breaks; do not interrupt prose without reason.",
    )
    allow_interrupt_long_explanations_for_frequency: bool = Field(
        description="May split or interrupt long explanations to meet KC cadence.",
    )


class EmbeddedKcFormat(BaseModel):
    """Structured shape of each embedded knowledge check."""

    typical_answer_option_count: int = Field(ge=2, le=8)
    components: list[str] = Field(
        min_length=1,
        description="Parts of each KC, e.g. stem, answer_options, correct_answer, explanation.",
    )
    allow_scenario_or_case_study: bool = False


class KcPlacementRules(BaseModel):
    """Rules governing where and how Knowledge Check questions are placed."""

    placement: str = Field(min_length=1, description="E.g. 'end_of_subtopic', 'per_section'.")
    min_kc_per_lesson: int = Field(ge=0)
    max_kc_per_lesson: int = Field(ge=0)
    min_answer_options: Optional[int] = Field(
        default=None,
        ge=2,
        le=8,
        description="Minimum answer options allowed for embedded knowledge checks (if constrained).",
    )
    max_answer_options: Optional[int] = Field(
        default=None,
        ge=2,
        le=8,
        description="Maximum answer options allowed for embedded knowledge checks (if constrained).",
    )
    forbidden_placements: list[str]
    require_explanation: bool = Field(description="Correct-answer explanation is mandatory.")
    distractor_quality: str = Field(min_length=1, description="E.g. 'plausible'.")
    cadence: Optional[KcCadence] = Field(
        None,
        description="Optional structured cadence (screens/pages between embedded KCs).",
    )
    placement_priorities: Optional[list[str]] = Field(
        None,
        description="Ordered or weighted placement priorities, as machine-readable tokens.",
    )
    interrupt_policy: Optional[KcInterruptPolicy] = Field(
        None,
        description="When KCs may interrupt instructional prose.",
    )
    avoid_kc_on: Optional[list[str]] = Field(
        None,
        description="Content types to avoid anchoring embedded KCs on (e.g. inflation-adjusted figures).",
    )
    kc_triggers: Optional[list[str]] = Field(
        None,
        description="Pedagogical moments that should trigger a knowledge check.",
    )
    embedded_kc_format: Optional[EmbeddedKcFormat] = Field(
        None,
        description="Structured description of embedded KC components and formats.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "placement": "end_of_subtopic",
                    "min_kc_per_lesson": 2,
                    "max_kc_per_lesson": 5,
                    "forbidden_placements": ["mid_paragraph", "after_table", "inside_regulatory_block"],
                    "require_explanation": True,
                    "distractor_quality": "plausible",
                }
            ]
        }
    }


# ── Deduplication Rules ───────────────────────────────────────────────────────

class DeduplicationRules(BaseModel):
    """Semantic similarity thresholds for detecting duplicate questions."""

    similarity_threshold: float = Field(gt=0.0, le=1.0)
    apply_between: list[str] = Field(
        min_length=1,
        description="Pairs to compare, e.g. 'KC_to_KC', 'KC_to_Exam'.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "similarity_threshold": 0.82,
                    "apply_between": ["KC_to_KC", "KC_to_Exam", "Exam_to_Exam"],
                }
            ]
        }
    }


# ── Lectora Constraints ───────────────────────────────────────────────────────

class LectoraConstraints(BaseModel):
    """Layout limits imposed by the Lectora authoring platform."""

    max_words_per_page: int = Field(gt=0)
    prefer_bulleted_content: bool
    allow_callouts: bool
    allow_tables: bool
    avoid_large_text_blocks: bool
    page_break_strategy: str = Field(min_length=1, description="E.g. 'subtopic_based'.")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "max_words_per_page": 180,
                    "prefer_bulleted_content": True,
                    "allow_callouts": True,
                    "allow_tables": True,
                    "avoid_large_text_blocks": True,
                    "page_break_strategy": "subtopic_based",
                }
            ]
        }
    }


# ── Error Tolerance ───────────────────────────────────────────────────────────

class ErrorTolerance(BaseModel):
    """Retry and tolerance settings for generation steps."""

    word_count_tolerance_percent: float = Field(gt=0, description="Allowed ± deviation from target word count.")
    retry_on_failure: bool
    max_retries_per_step: int = Field(ge=0)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"word_count_tolerance_percent": 10.0, "retry_on_failure": True, "max_retries_per_step": 3}
            ]
        }
    }


# ── IARCE learner / jurisdictional context (optional) ───────────────────────────


class DuallyRegisteredIarContext(BaseModel):
    """How dually registered IARs and regulatory emphasis shape E&PR course authoring."""

    estimated_dually_registered_share: float = Field(
        ge=0.0,
        le=1.0,
        description="Approximate fraction of IARs who are dually registered (e.g. BD + RIA).",
    )
    jurisdictions_relevant: list[str] = Field(
        min_length=1,
        description="Regulatory layers that may apply, as stable tokens (e.g. finra, state, sec).",
    )
    course_regulatory_emphasis: str = Field(
        min_length=1,
        description="Dominant regulatory framing for course content (e.g. sec_dominant).",
    )


# ── Root Rule Pack ────────────────────────────────────────────────────────────

class RulePack(BaseModel):
    """
    A complete rule pack governing one course family (Insurance CE, IARCE, Firm Element).

    Parsed from RULE_PACKS in rule_pack_config/rule_packs.py.
    """

    id: str = Field(min_length=1, description="Unique rule pack identifier, e.g. 'rp-insurance-ce-v3.1'.")
    family: str = Field(min_length=1, description="Human-readable family name, e.g. 'Insurance CE'.")
    version: str = Field(min_length=1, pattern=r"^\d+\.\d+$", description="Semver-style version, e.g. '3.1'.")
    guidance: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional categorized guidance used for prompt-building and documentation. "
            "Unlike structured rule fields, this is free-form JSON grouped by categories."
        ),
    )
    dually_registered_iar_context: Optional[DuallyRegisteredIarContext] = Field(
        None,
        description="IARCE only: dually registered audience mix and SEC vs other emphasis.",
    )
    assessment_rules: AssessmentRules
    style_constraints: StyleConstraints
    compliance_elements: ComplianceElements
    content_rules: ContentRules
    kc_placement_rules: KcPlacementRules
    deduplication_rules: DeduplicationRules
    lectora_constraints: LectoraConstraints
    error_tolerance: ErrorTolerance

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "id": "rp-insurance-ce-v3.1",
                    "family": "Insurance CE",
                    "version": "3.1",
                    "assessment_rules": {
                        "final_exam_min_questions": 15,
                        "answer_options_count": 4,
                        "allow_true_false": False,
                        "allow_all_of_the_above": False,
                        "forbidden_question_types": ["true_false", "all_of_the_above"],
                        "question_format_distribution": {
                            "scenario_based": 0.4,
                            "definition_based": 0.3,
                            "conceptual": 0.3,
                        },
                        "require_rationale": True,
                        "objective_coverage_required": True,
                    },
                    "style_constraints": {
                        "reading_level": "Grade 10-12",
                        "voice": "second_person",
                        "tone": "neutral_instructional_compliance",
                        "paragraph_length": "short",
                        "max_sentences_per_paragraph": 5,
                        "avoid_complex_jargon": True,
                        "explain_terms_on_first_use": True,
                        "bold_first_key_term": True,
                    },
                    "compliance_elements": {
                        "regulatory_mode": "safe_placeholder",
                        "require_non_advisory_language": True,
                        "forbidden_phrases": ["you should invest", "best option"],
                        "required_behaviors": ["use neutral explanations"],
                        "disclosure_handling": {
                            "allow_generic_regulatory_reference": True,
                            "no_hallucinated_citations": True,
                        },
                    },
                    "content_rules": {
                        "must_map_to_learning_objectives": True,
                        "no_duplicate_concepts_across_sections": True,
                        "no_unverified_statistics": True,
                        "no_opinion_based_statements": True,
                        "self_contained_subtopics": True,
                        "maintain_section_boundary_integrity": True,
                    },
                    "kc_placement_rules": {
                        "placement": "end_of_subtopic",
                        "min_kc_per_lesson": 2,
                        "max_kc_per_lesson": 5,
                        "forbidden_placements": ["mid_paragraph", "after_table"],
                        "require_explanation": True,
                        "distractor_quality": "plausible",
                    },
                    "deduplication_rules": {
                        "similarity_threshold": 0.82,
                        "apply_between": ["KC_to_KC", "KC_to_Exam"],
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
                        "word_count_tolerance_percent": 10.0,
                        "retry_on_failure": True,
                        "max_retries_per_step": 3,
                    },
                }
            ]
        }
    }
