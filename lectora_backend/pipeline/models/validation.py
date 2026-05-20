"""
Validator Pydantic models (S1 + S2).

Covers:
  - IssueSeverity    (blocker / warning / info)
  - ValidationIssue  (single check result; reused by S1 and S2)
  - S1Status         (pass / pass_with_warnings / blocked / blocker)
  - S1ValidationReport (full S1 output saved to shared state)
  - S2Status         (mirror of S1Status; used by content-stage validator)
  - S2ValidationReport (full S2 output saved to shared state)
"""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ── Severity ──────────────────────────────────────────────────────────────────

class IssueSeverity(str, Enum):
    """
    Four-tier severity system used by all S1/S2 check functions.

    - blocker  : pipeline MUST stop; A2 cannot proceed
    - critical : serious issue requiring immediate attention; study_guide review mandatory
    - warning  : flag for human review; processing may still continue
    - info     : informational only; no action required
    """

    blocker  = "blocker"
    critical = "critical"
    warning  = "warning"
    info     = "info"


# ── Validation Issue ──────────────────────────────────────────────────────────

class ValidationIssue(BaseModel):
    """
    A single check result emitted by any S1 check function.

    Maps directly to the dict shape returned by every function in
    `agent/s1_validator/utils/checks.py`.
    """

    field: str = Field(
        min_length=1,
        description="Dotted field path that failed, e.g. 'section.sec_001.heading'.",
    )
    expected: str = Field(
        min_length=1,
        description="Human-readable description of the expected value or condition.",
    )
    found: Any = Field(description="The actual value or condition that was found.")
    severity: IssueSeverity
    message: str = Field(min_length=1, description="Full human-readable explanation of the issue.")
    rule_source: str = Field(
        min_length=1,
        description="Rule key or module that triggered this check, e.g. 'kc_placement_rules.min_kc_per_lesson'.",
    )
    failure_reason: Optional[str] = Field(
        None,
        description="Short plain-language why this failed (especially blockers).",
    )
    remediation: Optional[str] = Field(
        None,
        description="Recommended next steps to resolve the issue.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "field": "knowledge_check_count",
                    "expected": ">= 16 (2/lesson x 8 lessons)",
                    "found": 8,
                    "severity": "warning",
                    "message": (
                        "Only 8 KCs found in source doc; rule pack requires >= 2 per lesson (16 total). "
                        "A2 will generate additional KCs to meet the requirement."
                    ),
                    "rule_source": "kc_placement_rules.min_kc_per_lesson",
                },
                {
                    "field": "title",
                    "expected": "non-empty course title",
                    "found": "''",
                    "severity": "blocker",
                    "message": "A0 failed to extract a course title from the document.",
                    "rule_source": "A0 metadata extraction",
                },
            ]
        }
    }


# ── S1 Status ─────────────────────────────────────────────────────────────────

class S1Status(str, Enum):
    """
    Final status of an S1 validation run.

    Note: early-exit paths (missing A1 output, missing rule pack) return the
    string literal ``"blocker"`` from S1Validator.run(); a full check cycle
    produces ``"blocked"``, ``"pass_with_warnings"``, or ``"pass"``.
    Both ``blocker`` and ``blocked`` prevent A2 from starting.
    """

    pass_ = "pass"
    pass_with_warnings = "pass_with_warnings"
    blocked = "blocked"
    blocker = "blocker"  # early-exit shorthand used in S1Validator.run()


# ── S1 Validation Report ──────────────────────────────────────────────────────

class S1ValidationReport(BaseModel):
    """
    Full S1 output stored in shared_state["s1_validation"] and
    persisted as `{run_id}_s1_validation.json`.

    Pipeline proceeds to A2 only when status is ``pass`` or ``pass_with_warnings``.
    """

    status: S1Status
    run_id: str = Field(min_length=1)
    issues: list[ValidationIssue] = Field(
        default_factory=list,
        description="All issues found, regardless of severity.",
    )
    blockers: int = Field(ge=0, description="Count of blocker-severity issues.")
    warnings: int = Field(ge=0, description="Count of warning-severity issues.")
    infos: int = Field(ge=0, description="Count of info-severity issues.")
    report_path: Optional[str] = Field(None, description="Path to the persisted JSON report file.")
    message: Optional[str] = Field(
        None,
        description="Short summary message, populated on early-exit paths.",
    )

    @model_validator(mode="after")
    def counts_match_issues(self) -> "S1ValidationReport":
        """Warn if the summary counts diverge from the actual issues list."""
        actual_blockers = sum(1 for i in self.issues if i.severity == IssueSeverity.blocker)
        actual_warnings = sum(1 for i in self.issues if i.severity == IssueSeverity.warning)
        actual_infos = sum(1 for i in self.issues if i.severity == IssueSeverity.info)

        if self.issues and (
            self.blockers != actual_blockers
            or self.warnings != actual_warnings
            or self.infos != actual_infos
        ):
            raise ValueError(
                f"Issue counts do not match the issues list. "
                f"Declared: blockers={self.blockers}, warnings={self.warnings}, infos={self.infos}. "
                f"Actual: blockers={actual_blockers}, warnings={actual_warnings}, infos={actual_infos}."
            )
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "pass_with_warnings",
                    "run_id": "69ecf0d5",
                    "issues": [
                        {
                            "field": "knowledge_check_count",
                            "expected": ">= 16",
                            "found": 8,
                            "severity": "warning",
                            "message": "Only 8 KCs found; A2 will generate more.",
                            "rule_source": "kc_placement_rules.min_kc_per_lesson",
                        }
                    ],
                    "blockers": 0,
                    "warnings": 1,
                    "infos": 2,
                    "report_path": "shared_state/69ecf0d5_s1_validation.json",
                    "message": None,
                }
            ]
        }
    }


# ── S2 Status ─────────────────────────────────────────────────────────────────

class S2Status(str, Enum):
    """
    Final status of an S2 (content-stage) validation run.

    Mirrors S1Status semantics:
    - pass / pass_with_warnings  → study_guide.docx may be rendered.
    - blocked / blocker          → study_guide.docx must NOT be rendered.
    """

    pass_ = "pass"
    pass_with_warnings = "pass_with_warnings"
    blocked = "blocked"
    blocker = "blocker"  # early-exit shorthand used in S2Validator.run()


# ── S2 Validation Report ──────────────────────────────────────────────────────

class S2ValidationReport(BaseModel):
    """
    Full S2 output stored in shared_state["s2_validation"] and persisted
    as `s2_validation.json`.

    The pipeline renders the study guide DOCX only when ``status`` is
    ``pass`` or ``pass_with_warnings``.
    """

    status: S2Status
    run_id: str = Field(min_length=1)
    issues: list[ValidationIssue] = Field(
        default_factory=list,
        description="All content-stage issues found, regardless of severity.",
    )
    blockers: int = Field(ge=0, description="Count of blocker-severity issues.")
    criticals: int = Field(ge=0, description="Count of critical-severity issues.")
    warnings: int = Field(ge=0, description="Count of warning-severity issues.")
    infos: int = Field(ge=0, description="Count of info-severity issues.")
    report_path: Optional[str] = Field(None, description="Path to the persisted JSON report file.")
    message: Optional[str] = Field(
        None,
        description="Short summary message, populated on early-exit paths.",
    )

    @model_validator(mode="after")
    def counts_match_issues(self) -> "S2ValidationReport":
        """Warn if the summary counts diverge from the actual issues list."""
        actual_blockers  = sum(1 for i in self.issues if i.severity == IssueSeverity.blocker)
        actual_criticals = sum(1 for i in self.issues if i.severity == IssueSeverity.critical)
        actual_warnings  = sum(1 for i in self.issues if i.severity == IssueSeverity.warning)
        actual_infos     = sum(1 for i in self.issues if i.severity == IssueSeverity.info)

        if self.issues and (
            self.blockers  != actual_blockers
            or self.criticals != actual_criticals
            or self.warnings  != actual_warnings
            or self.infos     != actual_infos
        ):
            raise ValueError(
                f"Issue counts do not match the issues list. "
                f"Declared: blockers={self.blockers}, criticals={self.criticals}, "
                f"warnings={self.warnings}, infos={self.infos}. "
                f"Actual: blockers={actual_blockers}, criticals={actual_criticals}, "
                f"warnings={actual_warnings}, infos={actual_infos}."
            )
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "pass_with_warnings",
                    "run_id": "69ecf0d5",
                    "issues": [
                        {
                            "field": "section.sec_004.knowledge_check.explanation",
                            "expected": "addresses each option (correct + incorrect)",
                            "found": "no reference to ['B', 'D']",
                            "severity": "warning",
                            "message": "Explanation does not appear to address every option.",
                            "rule_source": "assessment_rules.require_distractor_rationales",
                        }
                    ],
                    "blockers": 0,
                    "warnings": 1,
                    "infos": 0,
                    "report_path": "shared_state/69ecf0d5_s2_validation.json",
                    "message": None,
                }
            ]
        }
    }
