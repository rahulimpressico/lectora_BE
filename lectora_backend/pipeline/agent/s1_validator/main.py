"""
S1 — Stage 1 Validator

Validates A0 and A1 outputs against the active rule pack BEFORE content
generation (A2) begins. Acts as a quality gate in the pipeline.

Checks:
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

    def run(self) -> S1ValidationReport:
        """Execute all S1 validation checks and return a typed S1ValidationReport."""

        logger.info("[S1] Loading shared state for validation...")
        with open(self.shared_state_path) as f:
            shared_state = json.load(f)

        run_id = shared_state.get("run_id", "unknown")

        # ── Early exit: A1 not ready ─────────────────────────────────────
        a1_output = shared_state.get("agent_outputs", {}).get("A1", {})
        if not a1_output or a1_output.get("status") != "complete":
            issue = ValidationIssue(
                field="a1_output",
                expected="complete",
                found=a1_output.get("status", "missing") if a1_output else "missing",
                severity="blocker",
                message="A1 must complete before S1 validation.",
                rule_source="pipeline",
            )
            return S1ValidationReport(
                status=S1Status.blocker,
                run_id=run_id,
                message="A1 output not found or incomplete. Cannot validate.",
                issues=[issue],
                blockers=1,
                warnings=0,
                infos=0,
            )

        course_spec = a1_output.get("course_spec", {})

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

        logger.info("[S1] Checking rule pack sanity...")
        raw_issues.extend(check_rule_pack_sanity(rule_pack))

        logger.info("[S1] Checking A0 outputs...")
        raw_issues.extend(check_a0_metadata(shared_state))
        raw_issues.extend(check_a0_classification(shared_state))
        raw_issues.extend(check_a0_timed_outline_required(shared_state, rule_pack))
        raw_issues.extend(check_a0_images(shared_state))

        logger.info("[S1] Checking A1 outputs against rule pack...")
        raw_issues.extend(check_a1_sections(course_spec, rule_pack))
        raw_issues.extend(check_a1_word_counts(course_spec, rule_pack))
        raw_issues.extend(check_a1_kc_count(course_spec, rule_pack))
        raw_issues.extend(check_a1_learning_objectives_range(shared_state, rule_pack))
        raw_issues.extend(check_a1_lo_coverage(course_spec, shared_state, rule_pack))
        raw_issues.extend(check_a1_credit_hours(course_spec, shared_state))
        raw_issues.extend(check_a1_credit_hours_against_rule_pack(course_spec, shared_state, rule_pack))
        raw_issues.extend(check_a1_assessment_rules(course_spec, rule_pack))

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
        # Resolve so sidecars land next to the real shared_state file regardless of cwd
        output_dir = Path(self.shared_state_path).expanduser().resolve().parent
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

        shared_state["s1_validation"] = validation_dict
        shared_state["status"] = "S1_blocked" if status == S1Status.blocked else "S1_validated"

        with open(self.shared_state_path, "w") as f:
            json.dump(shared_state, f, indent=2, default=str)

        with open(report_path, "w") as f:
            json.dump(validation_dict, f, indent=2, default=str)

        logger.info("[S1] Report saved -> %s", report_path)
        return report
