"""
A1 — Outline Interpreter Pydantic models.

Covers:
  - ImagePlacement   (section-attached image subset)
  - SectionSpec      (single parsed section)
  - CourseSpec       (full A1 output structure)
  - Inconsistency    (structural warning from A1)
  - A1Output         (return value of A1 run())
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ── Image Placement ───────────────────────────────────────────────────────────

class ImagePlacement(BaseModel):
    """
    A subset of ImageRecord describing an image mapped to a specific section by A1.
    Extra keys from doc_parser (sha256, size_cm, alt_text, …) are silently ignored.
    """

    filename: str = Field(min_length=1)
    path: str = Field(min_length=1, description="Absolute path to the saved image file.")
    paragraph_index: Optional[int] = Field(None, ge=0, description="DOCX paragraph index where the image was found.")
    placement_note: Optional[str] = Field(None, description="Human-readable hint for Lectora placement.")

    model_config = {
        "extra": "ignore",
        "json_schema_extra": {
            "examples": [
                {
                    "filename": "image_003.png",
                    "path": "shared_state/69ecf0d5_images/image_003.png",
                    "paragraph_index": 87,
                    "placement_note": "Appears after the Risk Rating 2.0 table.",
                }
            ]
        }
    }


# ── Section Spec ──────────────────────────────────────────────────────────────

class SectionSpec(BaseModel):
    """
    A single section parsed by A1 from the study guide DOCX.

    Sections may be content blocks or Knowledge Check markers.
    Extra keys produced by A1 (e.g. 'paragraphs') are silently ignored.
    """

    id: str = Field(min_length=1, description="Stable identifier, e.g. 'sec_001'.")
    heading: str = Field(min_length=1, description="Section heading text as it appears in the document.")
    level: int = Field(ge=1, le=4, description="Heading level (1 = top-level lesson, 4 = minor sub-heading).")
    is_knowledge_check: bool = Field(False, description="True if this section is a KC placeholder, not a content block.")
    has_knowledge_check: bool = Field(False, description="True if a Knowledge Check was merged into this section.")
    para_start: Optional[int] = Field(None, ge=0, description="Index of the first paragraph in the DOCX.")
    para_end: Optional[int] = Field(None, ge=0, description="Index of the last paragraph in the DOCX.")
    subtopics: list[str] = Field(default_factory=list, description="Sub-headings detected within this section.")
    word_count: Optional[int] = Field(
        None,
        ge=0,
        description="Word count of the source text (optional; A1 may omit).",
    )
    estimated_duration_minutes: Optional[float] = Field(
        None,
        ge=0.0,
        description="Estimated learner minutes (optional; A1 may omit).",
    )
    interactive_elements: list[str] = Field(
        default_factory=list,
        description="Tagged interactive element types detected (e.g. 'knowledge_check', 'callout').",
    )
    maps_to_objectives: list[int] = Field(
        default_factory=list,
        description="Indices (0-based) into request_spec.extracted_inputs.learning_objectives.",
    )
    images: list[dict] = Field(
        default_factory=list,
        description="Images from the DOCX that fall within this section's paragraph range.",
    )
    image_count: int = Field(ge=0, default=0)

    @model_validator(mode="after")
    def sync_image_count(self) -> "SectionSpec":
        if self.image_count == 0 and self.images:
            self.image_count = len(self.images)
        return self

    model_config = {
        "extra": "ignore",
        "json_schema_extra": {
            "examples": [
                {
                    "id": "sec_001",
                    "heading": "Introduction to Flood Insurance",
                    "level": 1,
                    "is_knowledge_check": False,
                    "para_start": 5,
                    "para_end": 42,
                    "subtopics": ["History of the NFIP", "Program Goals"],
                    "word_count": 620,
                    "estimated_duration_minutes": 4.1,
                    "interactive_elements": ["callout"],
                    "maps_to_objectives": [0, 1],
                    "images": [],
                    "image_count": 0,
                }
            ]
        }
    }


# ── Course Spec ───────────────────────────────────────────────────────────────

class CourseSpec(BaseModel):
    """
    Full structured outline produced by A1.

    Persisted as `{run_id}_course_spec.json` and stored in
    shared_state["agent_outputs"]["A1"]["course_spec"].
    Extra keys produced by A1 (run_id, course_id, …) are silently preserved.
    """

    extracted_inputs: Optional[dict[str, Any]] = Field(
        None,
        description="Copied verbatim from shared_state.extracted_inputs: title, course_id, learning_objectives.",
    )
    sections: list[SectionSpec] = Field(min_length=1)

    # Optional aggregates — no longer emitted by A1; kept for backward compatibility
    # with older course_spec.json files.
    total_word_count: Optional[int] = Field(None, ge=0)
    total_duration_minutes: Optional[float] = Field(None, ge=0.0)
    credit_hours_derived: Optional[float] = Field(
        None,
        ge=0.0,
        description="Credit hours derived from word count by A1 (authoritative).",
    )
    credit_hours_a0: Optional[float] = Field(
        None,
        ge=0.0,
        description="Credit hours estimated by A0 LLM (informational cross-check).",
    )
    knowledge_check_count: Optional[int] = Field(
        None,
        ge=0,
        description="Number of KC sections detected in the source document.",
    )
    unassigned_images: Optional[list[dict]] = Field(
        None,
        description="Images that could not be mapped to any section.",
    )

    # Extra fields produced by A1's build_course_spec node
    run_id: Optional[str] = None
    course_id: Optional[str] = None
    course_title: Optional[str] = None
    interactive_element_summary: Optional[dict[str, int]] = None
    total_images: Optional[int] = Field(None, ge=0)

    model_config = {
        "extra": "ignore",
        "json_schema_extra": {
            "examples": [
                {
                    "sections": [
                        {
                            "id": "sec_001",
                            "heading": "Introduction to Flood Insurance",
                            "level": 1,
                            "is_knowledge_check": False,
                            "word_count": 620,
                            "estimated_duration_minutes": 4.1,
                            "maps_to_objectives": [0, 1],
                            "image_count": 0,
                        }
                    ],
                    "total_word_count": 12400,
                    "total_duration_minutes": 82.7,
                    "credit_hours_derived": 1.4,
                    "credit_hours_a0": 3,
                    "knowledge_check_count": 8,
                    "unassigned_images": [],
                }
            ]
        }
    }


# ── Inconsistency ─────────────────────────────────────────────────────────────

class InconsistencySeverity(str, Enum):
    warning = "warning"
    error = "error"
    info = "info"


class Inconsistency(BaseModel):
    """
    A structural issue detected by A1 during outline parsing (non-fatal).

    Blockers are escalated to S1; A1 records all issues here for transparency.
    Extra keys from A1's detect_inconsistencies (field, expected, found) are ignored.
    """

    severity: InconsistencySeverity
    message: str = Field(min_length=1)
    section_id: Optional[str] = Field(None, description="ID of the section where the issue was found.")
    # Additional fields A1 may include; typed Any because A1 puts
    # floats/ints here (e.g. credit_hours_a0, kc_found) not just strings
    field: Optional[str] = None
    expected: Optional[Any] = None
    found: Optional[Any] = None

    model_config = {
        "extra": "ignore",
        "json_schema_extra": {
            "examples": [
                {
                    "severity": "warning",
                    "message": "Section 'Risk Rating 2.0' has word_count=0 — may be a heading-only placeholder.",
                    "section_id": "sec_005",
                }
            ]
        }
    }


# ── A1 Output ─────────────────────────────────────────────────────────────────

class A1Status(str, Enum):
    complete = "complete"
    failed = "failed"
    error = "error"


class A1Output(BaseModel):
    """
    Return value of the A1 LangGraph run and the value stored in
    shared_state["agent_outputs"]["A1"].
    """

    status: A1Status
    course_spec: Optional[CourseSpec] = Field(
        None,
        description="Populated on success; None on failure.",
    )
    inconsistencies: list[Inconsistency] = Field(
        default_factory=list,
        description="Structural warnings detected during parsing.",
    )
    error: Optional[str] = Field(None, description="Error message when status != 'complete'.")
    retry_count: int = Field(ge=0, default=0, description="Number of A1 retries consumed so far.")
    timestamp: Optional[datetime] = Field(None, description="UTC time when A1 completed.")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "complete",
                    "course_spec": {
                        "sections": [],
                        "total_word_count": 12400,
                        "total_duration_minutes": 82.7,
                        "knowledge_check_count": 8,
                    },
                    "inconsistencies": [],
                    "error": None,
                    "retry_count": 0,
                    "timestamp": "2025-05-01T10:05:00+00:00",
                }
            ]
        }
    }
