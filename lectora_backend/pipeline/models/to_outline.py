"""
TO (Timed Outline) Pydantic models.

Mirrors the `TO_outline_format` template in rule_pack_config/timed_outline.py and the
LLM-extracted JSON produced by `classify_to_outline_with_llm` in A0.

All time/word-count fields are kept as `str | None` (not numeric) because
the LLM fills them in from a human-authored timed outline document where values
may appear as "4.5 min", "~620 words", or be left blank.
"""

from typing import Optional

from pydantic import BaseModel, Field


# ── TO Section ────────────────────────────────────────────────────────────────

class TOSection(BaseModel):
    """One section row extracted from the Timed Outline document."""

    title: str = Field(min_length=1, description="Lesson Topic column — section heading as written in the TO document.")
    content: str = Field(default="", description="Content Objective column — brief objective or description for this lesson (empty when blank in document).")
    topics: list[str] = Field(
        default_factory=list,
        description="Subtopic column split on newline — each subtopic or knowledge-check label is a separate list item.",
    )
    word_count: Optional[str] = Field(
        None,
        description="Word Count column — raw string as written, e.g. '4115'.",
    )
    minutes: Optional[str] = Field(
        None,
        description="Minutes column — estimated learner time, e.g. '23'.",
    )
    credit_hour: Optional[str] = Field(
        None,
        description="Credit Hour column — fractional credit hours, e.g. '.46'.",
    )
    interactive_elements: list[str] = Field(
        default_factory=list,
        description="Interactive Elements column split on comma, e.g. ['bulleted lists', 'knowledge checks']. 'n/a' entries omitted.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "title": "1.0 Coverage, Limits, and Rates",
                    "content": "",
                    "topics": ["1.1 Coverage", "1.2 Coverage Limits", "1.3 Rates", "Knowledge Check"],
                    "word_count": "4115",
                    "minutes": "23",
                    "credit_hour": ".46",
                    "interactive_elements": ["bulleted lists", "callouts", "knowledge checks"],
                }
            ]
        }
    }


# ── TO Totals ─────────────────────────────────────────────────────────────────

class TOTotals(BaseModel):
    """Aggregate totals row from the bottom of the Timed Outline document."""

    word_count: Optional[str] = Field(None, description="Total word count across all sections.")
    minutes: Optional[str] = Field(None, description="Total estimated learner time in minutes.")
    credit_hours: Optional[str] = Field(None, description="Total CE credit hours for the course.")

    model_config = {
        "json_schema_extra": {
            "examples": [{"word_count": "12400", "minutes": "82.7", "credit_hours": "1.38"}]
        }
    }


# ── TO Outline ────────────────────────────────────────────────────────────────

class TOOutline(BaseModel):
    """
    Structured representation of the Timed Outline document produced by A0's
    `classify_to_outline_with_llm` call.

    Stored in shared_state["llm_to_outline_classification"].

    Source document layout:
      Table 0  → course_title
      Table 1  → course_id
      Table 2  → description
      Table 3  → learning_objectives (newline-separated)
      Table 4  → 7-column outline grid (header + section rows + totals row)
    """

    course_title: str = Field(default="", description="Course title from the TO document.")
    course_id: str = Field(default="", description="Course ID from the TO document (e.g. '533').")
    description: str = Field(default="", description="Course description prose from the TO document.")
    learning_objectives: list[str] = Field(
        default_factory=list,
        description="Course-level learning objectives (one per line in the source document).",
    )
    sections: list[TOSection] = Field(
        default_factory=list,
        description="Ordered section rows from the 7-column outline table (excluding header and totals rows).",
    )
    totals: TOTotals = Field(default_factory=TOTotals)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "course_title": "Enhance Your Clients' Flood Insurance Experience",
                    "course_id": "533",
                    "description": "This course provides insurance professionals with a comprehensive understanding of flood insurance...",
                    "learning_objectives": [
                        "Explain what the NFIP covers, which limits apply, and how rates are set under Risk Rating 2.0",
                        "Identify flood risks in urban areas, including renters",
                    ],
                    "sections": [
                        {
                            "title": "1.0 Coverage, Limits, and Rates",
                            "content": "",
                            "topics": ["1.1 Coverage", "1.2 Coverage Limits", "1.3 Rates", "Knowledge Check"],
                            "word_count": "4115",
                            "minutes": "23",
                            "credit_hour": ".46",
                            "interactive_elements": ["bulleted lists", "callouts", "knowledge checks"],
                        }
                    ],
                    "totals": {"word_count": "10965", "minutes": "67.84", "credit_hours": "1.86"},
                }
            ]
        }
    }
