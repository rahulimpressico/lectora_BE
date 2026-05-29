"""
A0 — Request Synthesizer Pydantic models.

Covers:
  - CourseMetadata
  - RuleClassification
  - ResolvedAssessmentRules
  - RequestSpec
  - ProvenanceSource / ProvenanceEntry
  - ImageRecord
  - ExtractedInputs
  - LLMClassification
  - AgentOutputSlots
  - SharedState
  - A0OutputFiles / A0Result
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ── Course Metadata ───────────────────────────────────────────────────────────

class CourseMetadata(BaseModel):
    """Core course identifiers resolved by A0 and carried through all agents."""

    title: str = Field(min_length=1, description="Full course title extracted from the study guide.")
    course_id: Optional[str] = Field(None, description="Numeric or alphanumeric course identifier.")
    audience: Optional[str] = Field(None, description="Target learner profile, e.g. 'Insurance professionals'.")
    course_type: Optional[str] = Field(None, description="Delivery modality, e.g. 'Self-study CE'.")
    category: Optional[str] = Field(None, description="Subject category, e.g. 'Property & Casualty — Flood Insurance'.")
    topic: Optional[str] = Field(None, description="Primary topic inferred by LLM.")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "title": "Enhanced Flood Insurance Course",
                    "course_id": "533",
                    "audience": "Insurance professionals",
                    "course_type": "Self-study CE",
                    "category": "Property & Casualty — Flood Insurance",
                    "topic": "Flood insurance regulations",
                }
            ]
        }
    }


# ── Rule Classification ───────────────────────────────────────────────────────

class RuleClassification(BaseModel):
    """LLM-assigned rule pack reference attached to a run."""

    family: str = Field(min_length=1, description="Rule family name, e.g. 'Insurance CE'.")
    rule_pack_id: str = Field(min_length=1, description="E.g. 'rp-insurance-ce-v3.1'.")
    rule_pack_version: str = Field(min_length=1)
    llm_confidence: Optional[float] = Field(None, ge=0.0, le=1.0, description="Classifier confidence (0–1).")
    llm_reasoning: Optional[str] = Field(None, description="One-sentence LLM justification for the family choice.")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "family": "Insurance CE",
                    "rule_pack_id": "rp-insurance-ce-v3.1",
                    "rule_pack_version": "3.1",
                    "llm_confidence": 0.92,
                    "llm_reasoning": "The course covers NFIP flood insurance regulations targeting licensed agents.",
                }
            ]
        }
    }


# ── Resolved Assessment Rules ─────────────────────────────────────────────────

class ResolvedAssessmentRules(BaseModel):
    """
    Flat assessment parameters after merging explicit overrides, rule-pack defaults,
    and LLM inferences.  This is what A1 and A2 actually use.
    """

    exam_question_count: int = Field(gt=0)
    kc_per_section: int = Field(gt=0)
    min_kc_total: int = Field(gt=0)
    passing_score_pct: int = Field(gt=0, le=100)
    max_attempts: int = Field(gt=0)
    time_limit_minutes_per_question: float = Field(gt=0)
    question_types: list[str] = Field(min_length=1)
    bloom_levels: list[str] = Field(min_length=1)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "exam_question_count": 30,
                    "kc_per_section": 3,
                    "min_kc_total": 15,
                    "passing_score_pct": 70,
                    "max_attempts": 3,
                    "time_limit_minutes_per_question": 2.0,
                    "question_types": ["multiple_choice"],
                    "bloom_levels": ["remember", "understand", "apply"],
                }
            ]
        }
    }


# ── Request Spec ──────────────────────────────────────────────────────────────

class RequestSpec(BaseModel):
    """
    Normalised course specification produced by A0 and consumed by all downstream agents.

    Written to `{run_id}_request_spec.json` and embedded in SharedState.
    """

    run_id: str = Field(min_length=1, description="8-character UUID prefix uniquely identifying this pipeline run.")
    timestamp: datetime = Field(description="UTC time at which A0 produced this spec.")
    course_metadata: CourseMetadata
    rule_classification: RuleClassification

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "run_id": "69ecf0d5",
                    "timestamp": "2025-05-01T10:00:00+00:00",
                    "course_metadata": {
                        "title": "Enhanced Flood Insurance Course",
                        "course_id": "533",
                        "audience": "Insurance professionals",
                        "course_type": "Self-study CE",
                        "category": "Property & Casualty — Flood Insurance",
                        "topic": "Flood insurance regulations",
                    },
                    "rule_classification": {
                        "family": "Insurance CE",
                        "rule_pack_id": "rp-insurance-ce-v3.1",
                        "rule_pack_version": "3.1",
                        "llm_confidence": 0.92,
                        "llm_reasoning": "Course covers NFIP regulations.",
                    },
                }
            ]
        }
    }


# ── Provenance ────────────────────────────────────────────────────────────────

class ProvenanceSource(str, Enum):
    """How a resolved value was determined."""

    explicitly_provided = "explicitly_provided"
    derived_from_rule_pack = "derived_from_rule_pack"
    inferred = "inferred"
    unresolved = "unresolved"


class ProvenanceEntry(BaseModel):
    """Audit record for a single resolved parameter."""

    value: Any = Field(description="The resolved value (may be any JSON-serialisable type).")
    source: ProvenanceSource

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"value": 30, "source": "derived_from_rule_pack"},
                {"value": 6, "source": "explicitly_provided"},
            ]
        }
    }


# ── Image Record ──────────────────────────────────────────────────────────────

class ImageRecord(BaseModel):
    """Metadata for an image extracted from the source DOCX by A0."""

    filename: str = Field(min_length=1)
    path: str = Field(min_length=1, description="Absolute path to the saved image file.")
    width: Optional[int] = Field(None, ge=0, description="Image width in pixels, if available.")
    height: Optional[int] = Field(None, ge=0, description="Image height in pixels, if available.")
    format: Optional[str] = Field(None, description="File format, e.g. 'png', 'jpeg'.")
    paragraph_index: Optional[int] = Field(None, ge=0, description="Paragraph index in the DOCX where the image appears.")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "filename": "image_001.png",
                    "path": "/shared_state/69ecf0d5_images/image_001.png",
                    "width": 800,
                    "height": 600,
                    "format": "png",
                    "paragraph_index": 42,
                }
            ]
        }
    }


# ── Extracted Inputs ──────────────────────────────────────────────────────────

class ContentGenerationBounds(BaseModel):
    """LLM-estimated word-count range for content generation from a source document."""

    min: int = Field(ge=0, description="Minimum words the LLM can reliably generate from this source.")
    max: int = Field(ge=0, description="Maximum words the LLM can reasonably generate without over-expansion.")
    reasoning: Optional[str] = Field(None, description="LLM rationale for the bounds.")


class ExtractedInputs(BaseModel):
    """
    Raw inputs A0 pulls from all source documents (DOCX and/or PDF), enriched
    with structural extraction results.

    Shallow metadata (title, objectives, word counts) and deep structural data
    (heading tree, indexed content, TOC mapping) are both persisted here so that
    downstream agents and observability tooling see a consistent shared-state
    schema regardless of whether the sources are DOCX or PDF files.
    """

    # ── Shallow metadata ────────────────────────────────────────────────────
    title: str = Field(min_length=1)
    course_id: Optional[str] = None
    learning_objectives: list[str] = Field(description="Ordered list of learning objective strings.")
    content_sample: str = Field(
        min_length=0,
        description="Representative text excerpt used for LLM classification.",
    )
    total_doc_word_count: int = Field(
        default=0,
        ge=0,
        description="Total word count across all source documents (all paragraphs/blocks, no exclusions).",
    )
    content_generation_bounds: Optional[ContentGenerationBounds] = Field(
        None,
        description="LLM-estimated min/max word count the pipeline can generate from this source.",
    )
    to_outline_total_word_count: int = Field(
        default=0,
        ge=0,
        description="Total word count from the Timed Outline's 'totals' row — this is the authoritative generation target.",
    )

    # ── Structural extraction (consistent for DOCX and PDF sources) ─────────
    heading_tree: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Merged heading tree from all source documents. "
            "Each entry: {level: int, text: str, para_idx: int, source: str}."
        ),
    )
    heading_map: list[list[Any]] = Field(
        default_factory=list,
        description=(
            "Heading anchors for TO-to-source section mapping. "
            "Each entry: [para_idx, text, level] or [para_idx, text, level, source]."
        ),
    )
    indexed_content: str = Field(
        default="",
        description=(
            "[P<N>]-annotated paragraph/block content from all source documents "
            "(up to 8 000 words). Used by A0 for TO generation; persisted for audit."
        ),
    )
    toc_entries: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Table of Contents entries extracted from source documents. "
            "Each entry: {level: int, text: str, page: int|null, source: str}."
        ),
    )
    toc_section_contents: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "TOC entries mapped to body-content ranges with [P<N>] indexed excerpts. "
            "Each entry: {level, title, para_idx_start, para_idx_end, source, indexed_content}."
        ),
    )
    total_paragraphs: int = Field(
        default=0,
        ge=0,
        description="Total paragraph / block count across all source documents.",
    )
    paragraphs_by_source: dict[str, int] = Field(
        default_factory=dict,
        description="Paragraph / block count per source file, keyed by filename.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "title": "Enhanced Flood Insurance Course",
                    "course_id": "533",
                    "learning_objectives": [
                        "Describe the history and purpose of the NFIP.",
                        "Explain how Risk Rating 2.0 changes premium calculations.",
                    ],
                    "content_sample": "The National Flood Insurance Program (NFIP) was established...",
                    "total_doc_word_count": 12450,
                    "to_outline_total_word_count": 9800,
                    "heading_tree": [
                        {"level": 1, "text": "Introduction to NFIP", "para_idx": 5, "source": "study_guide.docx"}
                    ],
                    "heading_map": [[5, "Introduction to NFIP", 1]],
                    "indexed_content": "[P5] Introduction to NFIP\n[P6] The NFIP was established...",
                    "toc_entries": [],
                    "toc_section_contents": [],
                    "total_paragraphs": 280,
                    "paragraphs_by_source": {"study_guide.docx": 280},
                }
            ]
        }
    }


# ── LLM Classification ────────────────────────────────────────────────────────

class LLMClassification(BaseModel):
    """Raw output from the A0 rule-family classifier LLM call."""

    rule_family: str = Field(
        min_length=1,
        description="One of 'insurance_ce', 'iarce', or 'firm_element'.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    audience: Optional[str] = None
    course_type: Optional[str] = None
    category: Optional[str] = None
    topic: Optional[str] = None
    credit_hours_estimate: Optional[int] = Field(None, ge=1, description="LLM-inferred credit hours.")
    reasoning: Optional[str] = None

    @field_validator("rule_family")
    @classmethod
    def validate_rule_family(cls, v: str) -> str:
        allowed = {"insurance_ce", "iarce", "firm_element"}
        if v not in allowed:
            raise ValueError(f"rule_family must be one of {allowed}, got '{v}'.")
        return v

    model_config = {
        "extra": "ignore",
        "json_schema_extra": {
            "examples": [
                {
                    "rule_family": "insurance_ce",
                    "confidence": 0.92,
                    "audience": "Licensed insurance agents",
                    "course_type": "Self-study CE",
                    "category": "Property & Casualty — Flood Insurance",
                    "topic": "NFIP flood insurance regulations",
                    "credit_hours_estimate": 3,
                    "reasoning": "Course title and objectives focus on NFIP/flood insurance CE.",
                }
            ]
        }
    }


# ── Agent Output Slots ────────────────────────────────────────────────────────

class AgentOutputSlots(BaseModel):
    """
    Mutable slots in shared state where each agent writes its output.

    Values start as None and are populated as the pipeline progresses.
    Using `dict | None` keeps the schema flexible while agents are under development.
    """

    A1: Optional[dict[str, Any]] = None
    A2: Optional[dict[str, Any]] = None
    A3: Optional[dict[str, Any]] = None
    A4: Optional[dict[str, Any]] = None
    A5: Optional[dict[str, Any]] = None


# ── Shared State ──────────────────────────────────────────────────────────────

class PipelineStatus(str, Enum):
    """Lifecycle status values written to shared state by the pipeline orchestrator."""

    initialised = "initialised"
    a1_running = "a1_running"
    s1_validated = "S1_validated"
    s1_blocked = "S1_blocked"
    a2_complete = "A2_complete"
    failed = "failed"


class SharedState(BaseModel):
    """
    Central shared state document persisted as `{run_id}_shared_state.json`.

    Every agent reads from and writes to this structure.
    """

    run_id: str = Field(min_length=1)
    status: str = Field(description="Current pipeline lifecycle stage (see PipelineStatus).")
    request_spec: RequestSpec
    provenance_log: dict[str, ProvenanceEntry] = Field(
        description="Audit log mapping each resolved parameter name to its value and source.",
    )
    source_document: str = Field(min_length=1, description="Basename of the input DOCX file.")
    extracted_inputs: ExtractedInputs
    images: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Raw image records from doc_parser (id, saved_path, para_idx, …).",
    )
    llm_classification: LLMClassification
    llm_to_outline_classification: Optional[dict[str, Any]] = Field(
        None,
        description="Structured TO-outline extraction from the timed outline DOCX.",
    )
    agent_outputs: AgentOutputSlots = Field(default_factory=AgentOutputSlots)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "run_id": "69ecf0d5",
                    "status": "initialised",
                    "request_spec": {"run_id": "69ecf0d5", "timestamp": "2025-05-01T10:00:00+00:00"},
                    "provenance_log": {
                        "exam_question_count": {"value": 30, "source": "derived_from_rule_pack"}
                    },
                    "source_document": "533_Enhanced Flood Insurance Course_SG_2025.05.docx",
                    "extracted_inputs": {
                        "title": "Enhanced Flood Insurance Course",
                        "learning_objectives": ["Describe the history and purpose of the NFIP."],
                        "content_sample": "The NFIP was established in 1968...",
                    },
                    "images": [],
                    "llm_classification": {
                        "rule_family": "insurance_ce",
                        "confidence": 0.92,
                    },
                    "agent_outputs": {"A1": None, "A2": None, "A3": None, "A4": None, "A5": None},
                }
            ]
        }
    }


# ── A0 Result ─────────────────────────────────────────────────────────────────

class A0OutputFiles(BaseModel):
    """Paths to JSON artefacts written by A0."""

    request_spec: str = Field(min_length=1)
    provenance_log: str = Field(min_length=1)
    shared_state: str = Field(min_length=1)
    llm_to_outline: str = Field(
        min_length=1,
        description="Sidecar: classify_to_outline_with_llm() output with pacing metrics enriched (JSON).",
    )
    llm_to_outline_raw: str = Field(
        default="",
        description="Unmodified copy saved as llm_to_outline_COPY.json before pacing-metrics enrichment.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "request_spec": "shared_state/69ecf0d5_request_spec.json",
                    "provenance_log": "shared_state/69ecf0d5_provenance_log.json",
                    "shared_state": "shared_state/69ecf0d5_shared_state.json",
                    "llm_to_outline": "shared_state/533_llm_to_outline.json",
                }
            ]
        }
    }


class A0Result(BaseModel):
    """Return value of A0RequestSynthesizer.run()."""

    request_spec: RequestSpec
    provenance_log: dict[str, ProvenanceEntry]
    shared_state_path: str = Field(min_length=1)
    output_files: A0OutputFiles
    llm_to_outline: dict[str, Any] | None = Field(
        default=None,
        description="In-memory TO outline dict (llm_to_outline key) for fast API responses.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "request_spec": {"run_id": "69ecf0d5"},
                    "provenance_log": {},
                    "shared_state_path": "shared_state/69ecf0d5_shared_state.json",
                    "output_files": {
                        "request_spec": "shared_state/69ecf0d5_request_spec.json",
                        "provenance_log": "shared_state/69ecf0d5_provenance_log.json",
                        "shared_state": "shared_state/69ecf0d5_shared_state.json",
                        "llm_to_outline": "shared_state/69ecf0d5_llm_to_outline.json",
                    },
                }
            ]
        }
    }
