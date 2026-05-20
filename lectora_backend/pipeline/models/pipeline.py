"""
Pipeline orchestrator Pydantic models.

Covers:
  - PipelineResult — structured return value describing the outcome of
    a complete run_pipeline() execution (all stages included).
"""

from typing import Optional

from pydantic import BaseModel, Field

from .content import A2Output
from .course import A0Result
from .outline import A1Output
from .validation import S1ValidationReport, S2ValidationReport


class PipelineResult(BaseModel):
    """
    Structured result of a complete ``run_pipeline()`` execution.

    Fields for agents that did not run (e.g. A2 skipped because S1 was
    blocked) are set to ``None``.
    """

    run_id: str = Field(
        min_length=1,
        description="8-character run identifier shared across all artefacts.",
    )
    shared_state_path: str = Field(
        min_length=1,
        description="Absolute path to the shared_state.json file for this run.",
    )
    a0: A0Result = Field(
        description="A0 output; always present (A0 must succeed for the pipeline to start).",
    )
    a1: Optional[A1Output] = Field(
        None,
        description="A1 output; None if A1 did not complete.",
    )
    s1: Optional[S1ValidationReport] = Field(
        None,
        description="S1 output; None if S1 was skipped.",
    )
    sections_mapped: Optional[int] = Field(
        None,
        description="Number of enriched sections produced by the Section Mapper; None if the mapper did not run.",
    )
    a2: Optional[A2Output] = Field(
        None,
        description="A2 output; None if A2 was skipped or blocked.",
    )
    s2: Optional[S2ValidationReport] = Field(
        None,
        description="S2 output; None if S2 was skipped.",
    )
    study_guide_path: Optional[str] = Field(
        None,
        description="Absolute path to the rendered study_guide.docx; None if rendering was skipped.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "run_id": "69ecf0d5",
                    "shared_state_path": "/…/shared_state/69ecf0d5_shared_state.json",
                    "a0": {"request_spec": {"run_id": "69ecf0d5"}},
                    "a1": {
                        "status": "complete",
                        "course_spec": {
                            "sections": [],
                            "total_word_count": 12400,
                            "knowledge_check_count": 8,
                        },
                        "inconsistencies": [],
                        "retry_count": 0,
                    },
                    "s1": {
                        "status": "pass_with_warnings",
                        "run_id": "69ecf0d5",
                        "issues": [],
                        "blockers": 0,
                        "warnings": 1,
                        "infos": 2,
                    },
                    "sections_mapped": 14,
                    "a2": {
                        "status": "complete",
                        "stats": {
                            "generated": 14,
                            "skipped": 0,
                            "failed": 0,
                            "total_words": 11800,
                        },
                    },
                    "s2": {
                        "status": "pass",
                        "run_id": "69ecf0d5",
                        "issues": [],
                        "blockers": 0,
                        "warnings": 0,
                        "infos": 0,
                    },
                    "study_guide_path": "/…/shared_state/69ecf0d5_study_guide.docx",
                }
            ]
        }
    }
