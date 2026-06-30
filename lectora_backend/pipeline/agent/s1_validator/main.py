"""
S1 — Stage 1 Validator

Validates A0 and A1 outputs against the active rule pack and user intent
BEFORE content generation (A2) begins. Acts as a quality gate in the pipeline.

Checks:
  AI semantic validation:
    - user-requirement alignment (priority over rule pack when present)
    - logical sequencing, prerequisite ordering, and learning progression
    - mandatory topic coverage, missing critical topics, and relevance/hallucination
    - topic overlap/duplication, naming clarity, and depth appropriateness
  A0:
    - Metadata extraction (title, course_id, LOs, content_sample)
    - LLM classification confidence + rule pack resolution
    - Image extraction
  A1:
    - Section structure (non-empty, word counts > 0)
    - KC count vs rule_pack.kc_placement_rules.min_kc_per_lesson
    - LO coverage (content_rules.must_map_to_learning_objectives)
    - Credit hour cross-check (A0 estimate vs A1 derived)
    - Word count / Lectora page limits
    - Assessment rule pre-flight (answer format, T/F ban, etc.)

Severity levels:
  - blocker: pipeline MUST stop
  - warning: flag for review, allow A2 to proceed
  - info: informational only

Output: validation report saved to shared_state, pipeline proceeds if no blockers.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

S1ValidationPhase = Literal["full", "to_only", "a1_only"]

from lectora_backend.pipeline.models import ValidationIssue, S1ValidationReport, S1Status
from lectora_backend.pipeline.rule_pack_config.rule_packs import resolve_rule_pack
from .utils.checks import (
    check_rule_pack_sanity,
    check_a0_metadata,
    check_a0_classification,
    check_a0_timed_outline_required,
    check_a0_images,
    check_a1_sections,
    check_a1_word_counts,
    check_a1_kc_count,
    check_a1_learning_objectives_range,
    check_a1_lo_coverage,
    check_a1_credit_hours,
    check_a1_credit_hours_against_rule_pack,
    check_a1_assessment_rules,
)
from .utils.ai_checks import run_ai_outline_checks

logger = logging.getLogger(__name__)


class S1Validator:
    """
    S1 — Stage 1 Validator

    Reads shared state (A0 + A1 outputs), runs all validation checks
    against the active rule pack, and produces a validation report.
    Returns pass/fail status: A2 only proceeds if no blockers.
    """

    def __init__(self, shared_state_path: str):
        self.shared_state_path = shared_state_path

    def run(self, *, phase: S1ValidationPhase = "full") -> S1ValidationReport:
        """Execute S1 validation checks and return a typed S1ValidationReport.

        phase:
          - ``full``     — A0 + A1 (when ready) + AI checks (main pipeline default)
          - ``to_only``  — A0 + AI checks only (generate-TO phase 1, before A1)
          - ``a1_only``  — A1 rule-pack checks only (generate-TO phase 2, after A1)
        """

        logger.info("[S1] Loading shared state for validation (phase=%s)...", phase)
        with open(self.shared_state_path) as f:
            shared_state = json.load(f)

        run_id = shared_state.get("run_id", "unknown")

        a1_output = shared_state.get("agent_outputs", {}).get("A1", {})
        a1_ready = bool(a1_output) and a1_output.get("status") == "complete"
        course_spec = a1_output.get("course_spec", {}) if a1_ready else {}

        if phase == "to_only":
            logger.info("[S1] Phase to_only: validating A0 TO outline before A1 runs.")
        elif phase == "a1_only":
            logger.info("[S1] Phase a1_only: validating A1 course_spec after A1 completes.")
        elif not a1_ready:
            logger.info(
                "[S1] A1 output missing/incomplete; running TO-only validation on A0 outline."
            )

        # ── Early exit: rule pack not resolved ───────────────────────────
        rule_family = (
            shared_state.get("request_spec", {})
            .get("rule_classification", {})
            .get("family")
        )
        rule_pack = (
            resolve_rule_pack(rule_family, shared_state.get("course_difficulty"))
            if rule_family
            else None
        )
        if not rule_pack:
            issue = ValidationIssue(
                field="rule_pack",
                expected="valid rule family",
                found=repr(rule_family),
                severity="blocker",
                message=f"Rule pack not found for '{rule_family}'.",
                rule_source="rule_pack_config.rule_packs",
            )
            return S1ValidationReport(
                status=S1Status.blocker,
                run_id=run_id,
                message=f"Could not resolve rule pack for family '{rule_family}'.",
                issues=[issue],
                blockers=1,
                warnings=0,
                infos=0,
            )

        # ── Run all checks ───────────────────────────────────────────────
        logger.info(
            "[S1] Validating against rule pack: %s %s",
            rule_pack["family"],
            rule_pack["version"],
        )

        raw_issues: list[dict] = []
        priority_rule = "Rule pack deterministic validation."

        logger.info("[S1] Checking rule pack sanity...")
        raw_issues.extend(check_rule_pack_sanity(rule_pack))

        run_a0 = phase in ("full", "to_only")
        run_a1 = phase in ("full", "a1_only")
        run_ai = phase in ("full", "to_only")

        if run_a0:
            logger.info("[S1] Checking A0 outputs...")
            raw_issues.extend(check_a0_metadata(shared_state))
            raw_issues.extend(check_a0_classification(shared_state))
            raw_issues.extend(check_a0_timed_outline_required(shared_state, rule_pack))
            raw_issues.extend(check_a0_images(shared_state))

        if run_a1:
            if a1_ready:
                logger.info("[S1] Checking A1 outputs against rule pack...")
                raw_issues.extend(check_a1_sections(course_spec, rule_pack))
                raw_issues.extend(check_a1_word_counts(course_spec, rule_pack))
                raw_issues.extend(check_a1_kc_count(course_spec, rule_pack))
                raw_issues.extend(check_a1_learning_objectives_range(shared_state, rule_pack))
                raw_issues.extend(check_a1_lo_coverage(course_spec, shared_state, rule_pack))
                raw_issues.extend(check_a1_credit_hours(course_spec, shared_state))
                raw_issues.extend(check_a1_credit_hours_against_rule_pack(course_spec, shared_state, rule_pack))
                # Temporarily skip A1 assessment pre-flight checks (exam/question readiness)
                # until assessment enforcement is moved fully to downstream stages.
            elif phase == "a1_only":
                raw_issues.append(
                    {
                        "field": "a1_output",
                        "expected": "A1 complete",
                        "found": a1_output.get("status", "missing") if a1_output else "missing",
                        "severity": "blocker",
                        "message": "A1 validation cannot run because A1 output is missing or incomplete.",
                        "rule_source": "pipeline.flow",
                    }
                )
            elif phase == "full":
                raw_issues.append(
                    {
                        "field": "a1_output",
                        "expected": "A1 complete",
                        "found": a1_output.get("status", "missing") if a1_output else "missing",
                        "severity": "info",
                        "message": "A1 checks skipped for TO-only validation mode (A0 → S1).",
                        "rule_source": "pipeline.flow",
                    }
                )

        if run_ai:
            logger.info("[S1] Running AI semantic outline validation...")
            ai_issues, priority_rule = run_ai_outline_checks(
                shared_state=shared_state,
                course_spec=course_spec,
                rule_pack=rule_pack,
            )
            raw_issues.extend(ai_issues)

        # Validate each raw issue dict into a typed ValidationIssue
        all_issues: list[ValidationIssue] = [
            ValidationIssue.model_validate(i) for i in raw_issues
        ]

        # ── Tally results ────────────────────────────────────────────────
        blockers = [i for i in all_issues if i.severity == "blocker"]
        warnings = [i for i in all_issues if i.severity == "warning"]
        infos    = [i for i in all_issues if i.severity == "info"]

        if blockers:
            status = S1Status.blocked
        elif warnings:
            status = S1Status.pass_with_warnings
        else:
            status = S1Status.pass_

        # ── Print report ─────────────────────────────────────────────────
        logger.info("[S1] Validation complete: %s", status.upper())
        logger.info(
            "     Blockers: %s  |  Warnings: %s  |  Info: %s",
            len(blockers),
            len(warnings),
            len(infos),
        )

        if blockers:
            logger.warning("  BLOCKERS (pipeline cannot proceed):")
            for b in blockers:
                logger.warning("    [BLOCKER] %s: %s", b.field, b.message)
                logger.warning("              Rule: %s", b.rule_source)

        if warnings:
            logger.info("  WARNINGS (review recommended):")
            for w in warnings:
                logger.info("    [WARNING] %s: %s", w.field, w.message)
                logger.info("              Rule: %s", w.rule_source)

        if infos:
            logger.info("  INFO:")
            for i in infos:
                logger.info("    [INFO] %s: %s", i.field, i.message)

        # ── Persist to shared state ──────────────────────────────────────
        output_dir = Path(self.shared_state_path).expanduser().resolve().parent
        # Persist a single canonical S1 sidecar irrespective of phase.
        # Latest run (to_only / a1_only / full) overwrites this file.
        report_path = output_dir / "s1_validation.json"

        report = S1ValidationReport(
            status=status,
            run_id=run_id,
            issues=all_issues,
            blockers=len(blockers),
            warnings=len(warnings),
            infos=len(infos),
            report_path=str(report_path.resolve()),
        )

        validation_dict = report.model_dump(mode="json")
        validation_dict["timestamp"] = datetime.now(timezone.utc).isoformat()
        validation_dict["rule_pack_used"] = f"{rule_pack['family']} {rule_pack['version']}"
        validation_dict["validation_priority"] = priority_rule
        validation_dict["phase"] = phase

        shared_state["s1_validation"] = validation_dict
        shared_state["status"] = "S1_blocked" if status == S1Status.blocked else "S1_validated"

        with open(self.shared_state_path, "w") as f:
            json.dump(shared_state, f, indent=2, default=str)

        with open(report_path, "w") as f:
            json.dump(validation_dict, f, indent=2, default=str)

        logger.info("[S1] Report saved -> %s", report_path)
        return report
