"""
A2 — Content Generator Pydantic models.

Covers:
  - Body-paragraph discriminated union (one model per `type` value)
  - BodyParagraph  — the Union type alias used everywhere
  - GeneratedSection
  - A2Stats
  - A2Output
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# ── Body Paragraph Discriminated Union ────────────────────────────────────────
# Each concrete model maps to one of the eight types defined in
# SECTION_SYSTEM in agent/a2_content_generator/prompt/section_prompt.py.
# Pydantic uses the `type` literal field as the discriminator key.

class TextBlock(BaseModel):
    """Standard body paragraph (Bar Text in Lectora)."""

    type: Literal["text"]
    content: str = Field(min_length=1, description="Paragraph prose; max ~180 words per Lectora page limit.")

    model_config = {
        "json_schema_extra": {
            "examples": [{"type": "text", "content": "The **National Flood Insurance Program (NFIP)** was established in 1968 to reduce the impact of flooding on private and public structures."}]
        }
    }


class BulletListBlock(BaseModel):
    """Bulleted list of items."""

    type: Literal["bullet_list"]
    items: list[str] = Field(min_length=1, description="Each string is one bullet item.")

    model_config = {
        "json_schema_extra": {
            "examples": [{"type": "bullet_list", "items": ["Standard flood policies cover direct physical loss.", "Contents coverage is purchased separately."]}]
        }
    }


class SubBulletListBlock(BaseModel):
    """Indented sub-bullets under a parent bullet point."""

    type: Literal["sub_bullet_list"]
    items: list[str] = Field(min_length=1)

    model_config = {
        "json_schema_extra": {
            "examples": [{"type": "sub_bullet_list", "items": ["Building coverage: up to $250,000", "Contents coverage: up to $100,000"]}]
        }
    }


class NumberedListBlock(BaseModel):
    """Ordered numbered list."""

    type: Literal["numbered_list"]
    items: list[str] = Field(min_length=1)

    model_config = {
        "json_schema_extra": {
            "examples": [{"type": "numbered_list", "items": ["Obtain an Elevation Certificate.", "Submit the FEMA application.", "Wait for the 30-day waiting period."]}]
        }
    }


class ImportantCalloutBlock(BaseModel):
    """
    Highlighted key-concept box rendered with a lavender background in Lectora.

    Use sparingly: 1–2 per section maximum.
    """

    type: Literal["important_callout"]
    content: str = Field(min_length=1, description="Short key takeaway text (1–3 sentences).")
    label: Optional[str] = Field(
        None,
        description="Emphasis type from rule pack (e.g. Important, Pro Tip, Warning). Rendered bold in output.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "type": "important_callout",
                    "label": "Pro Tip",
                    "content": "Risk Rating 2.0 replaces flood-zone maps with property-level assessment, which changes how you explain premiums to clients.",
                }
            ]
        }
    }


class KnowledgeCheckBlock(BaseModel):
    """
    Focus/Discussion question block — a 4-option MCQ (A–D).

    Constraints enforced by the rule pack:
    - Exactly 4 options.
    - No True/False.
    - No "All of the above".
    - Distractors must be plausible.
    - Correct answer must include an explanation.
    """

    type: Literal["knowledge_check"]
    question: str = Field(min_length=10, description="Full question stem.")
    options: list[str] = Field(
        min_length=4,
        max_length=4,
        description="Exactly four answer options, each prefixed 'A) ', 'B) ', 'C) ', 'D) '.",
    )
    correct_answer: str = Field(
        pattern=r"^[A-D]$",
        description="Single uppercase letter identifying the correct option.",
    )
    explanation: str = Field(
        min_length=10,
        description="Rationale explaining why the correct answer is right.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "type": "knowledge_check",
                    "question": "Which of the following best describes the primary purpose of the NFIP?",
                    "options": [
                        "A) To provide tax incentives for flood-resistant construction.",
                        "B) To offer federally backed flood insurance and reduce disaster relief costs.",
                        "C) To regulate state-level flood-zone mapping standards.",
                        "D) To eliminate the need for private flood insurance.",
                    ],
                    "correct_answer": "B",
                    "explanation": "The NFIP was created to provide affordable flood insurance through the federal government and reduce the burden on taxpayer-funded disaster relief.",
                }
            ]
        }
    }


class Heading3Block(BaseModel):
    """Sub-heading within a section (H3 in Lectora)."""

    type: Literal["heading_3"]
    content: str = Field(min_length=1)

    model_config = {
        "json_schema_extra": {
            "examples": [{"type": "heading_3", "content": "Flood Zone Designations"}]
        }
    }


class Heading4Block(BaseModel):
    """Minor sub-heading within a section (H4 in Lectora)."""

    type: Literal["heading_4"]
    content: str = Field(min_length=1)

    model_config = {
        "json_schema_extra": {
            "examples": [{"type": "heading_4", "content": "Zone AE vs Zone X"}]
        }
    }


# Discriminated union — Pydantic resolves the correct model via the `type` field.
BodyParagraph = Annotated[
    Union[
        TextBlock,
        BulletListBlock,
        SubBulletListBlock,
        NumberedListBlock,
        ImportantCalloutBlock,
        KnowledgeCheckBlock,
        Heading3Block,
        Heading4Block,
    ],
    Field(discriminator="type"),
]


# ── Generated Section ─────────────────────────────────────────────────────────

class GeneratedSectionStatus(str, Enum):
    generated = "generated"
    skipped = "skipped"
    skipped_thin = "skipped_thin"   # thin heading-only sections, set by content_writer
    failed = "failed"


class GeneratedSection(BaseModel):
    """
    One section of LLM-generated content produced by A2.

    Stored in `{run_id}_generated_content.json` under the ``sections`` key.
    """

    section_id: Optional[str] = Field(None, description="Matches SectionSpec.id; None for skipped thin headings.")
    heading: str = Field(min_length=1)
    level: int = Field(ge=1, le=4)
    is_knowledge_check: bool = False
    body_paragraphs: list[BodyParagraph] = Field(
        default_factory=list,
        description="Ordered sequence of typed content blocks.",
    )
    word_count: int = Field(ge=0)
    status: GeneratedSectionStatus
    subtopics: list[str] = Field(default_factory=list)
    maps_to_objectives: list[int] = Field(default_factory=list)
    images: list[dict] = Field(
        default_factory=list,
        description="Image placement hints inherited from the A1 SectionSpec.",
    )
    word_count_warning: Optional[str] = Field(
        None,
        description="Non-None when the generated word count deviates > 10% from target.",
    )
    attempts: int = Field(ge=1, default=1, description="Number of LLM generation attempts consumed.")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "section_id": "sec_001",
                    "heading": "Introduction to Flood Insurance",
                    "level": 1,
                    "is_knowledge_check": False,
                    "body_paragraphs": [
                        {"type": "text", "content": "The **NFIP** was established in 1968..."},
                        {"type": "bullet_list", "items": ["Standard policies cover buildings.", "Contents are separate."]},
                        {"type": "important_callout", "content": "Flood damage is excluded from standard homeowner policies."},
                        {
                            "type": "knowledge_check",
                            "question": "What does the NFIP primarily provide?",
                            "options": ["A) Tax breaks", "B) Federally backed flood insurance", "C) State mapping", "D) Private reinsurance"],
                            "correct_answer": "B",
                            "explanation": "The NFIP offers affordable flood coverage backed by the federal government.",
                        },
                    ],
                    "word_count": 580,
                    "status": "generated",
                    "subtopics": ["History of the NFIP"],
                    "maps_to_objectives": [0, 1],
                    "images": [],
                    "word_count_warning": None,
                    "attempts": 1,
                }
            ]
        }
    }


# ── A2 Stats ──────────────────────────────────────────────────────────────────

class A2Stats(BaseModel):
    """Aggregated generation statistics returned by A2ContentGenerator.run()."""

    generated: int = Field(ge=0, description="Sections successfully generated.")
    skipped: int = Field(ge=0, description="Thin heading-only sections intentionally skipped.")
    failed: int = Field(ge=0, description="Sections where all generation attempts failed.")
    total_words: int = Field(ge=0, description="Total words across all generated sections.")

    model_config = {
        "json_schema_extra": {
            "examples": [{"generated": 18, "skipped": 3, "failed": 0, "total_words": 9840}]
        }
    }


# ── A2 Output ─────────────────────────────────────────────────────────────────

class A2Output(BaseModel):
    """Return value of A2ContentGenerator.run()."""

    status: str = Field(min_length=1, description="E.g. 'complete', 'partial', 'failed'.")
    run_id: str = Field(min_length=1)
    course_title: str = Field(min_length=1)
    # Raw dicts kept flexible — LLM output may have unexpected block types
    sections: list[dict[str, Any]] = Field(default_factory=list)
    stats: A2Stats
    # LLM-generated front/back matter stored so downstream consumers (API, editor)
    # can read them from shared_state without re-invoking the LLM.
    course_description: str = Field(default="", description="LLM-generated course overview (1.0 OVERVIEW in DOCX).")
    course_conclusion: str = Field(default="", description="LLM-generated conclusion section (end of DOCX).")
    study_guide_docx: Optional[str] = Field(None, description="Path to the generated DOCX study guide.")
    generated_content_json: Optional[str] = Field(None, description="Path to the generated content JSON sidecar.")
    timestamp: Optional[datetime] = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "complete",
                    "run_id": "69ecf0d5",
                    "course_title": "Enhanced Flood Insurance Course",
                    "sections": [],
                    "stats": {"generated": 18, "skipped": 3, "failed": 0, "total_words": 9840},
                    "study_guide_docx": "shared_state/69ecf0d5_study_guide.docx",
                    "generated_content_json": "shared_state/69ecf0d5_generated_content.json",
                    "timestamp": "2025-05-01T10:30:00+00:00",
                }
            ]
        }
    }
