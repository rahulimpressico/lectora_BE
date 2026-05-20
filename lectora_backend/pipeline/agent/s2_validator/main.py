"""
S2 — Stage 2 Validator

Validates A2 generated content against the active rule pack BEFORE the
study guide DOCX is rendered. Acts as a quality gate between content
generation (A2) and final document assembly.

Inputs:
  - shared_state.json (single source of truth — reads A2 output from
    ``shared_state["agent_outputs"]["A2"]``; the standalone
    ``generated_content.json`` sidecar is NOT consulted)
  - rule pack (resolved from A0 classification)

Checks:
  - A2 completeness (no failed sections, sections present)
  - Section non-emptiness
  - Knowledge-check structure (option counts, correct_answer, explanation)
  - Forbidden question types (T/F, AOTA, NOTA, except, roman numeral)
  - Distractor rationales (when required)
  - KC placement per lesson (min/max, forbidden placements)
  - Forbidden phrases scan (compliance_elements)
  - Lectora page-size sanity
  - Word-count target vs TO totals (error_tolerance)
  - LO coverage in generated content

Severity:
  - blocker → study_guide.docx must NOT be produced
  - warning → review recommended; docx may still be produced
  - info    → informational only

Output: validation report saved to shared_state and as ``s2_validation.json``.
  Blocker rows may include ``failure_reason`` and ``remediation`` from an LLM (``top_k=1``).
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from lectora_backend.pipeline.models import (
    S2Status,
    S2ValidationReport,
    ValidationIssue,
)
from lectora_backend.pipeline.rule_pack_config.rule_packs import resolve_rule_pack
from .utils.checks import (
    check_a2_completeness,
    check_callouts_per_section,
    check_course_word_count_bands,
    check_examples_per_section,
    check_forbidden_phrases,
    check_required_behaviors,
    check_intro_section,
    check_kc_distractor_rationales,
    check_kc_placement,
    check_kc_structure,
    check_lectora_page_limits,
    check_lo_coverage,
    check_los_in_first_section,
    check_no_duplicate_headings,
    check_regulatory_mode,
    check_section_non_empty,
    check_summary_section,
    check_voice_pronouns,
    check_word_count_against_doc_bounds,
)

logger = logging.getLogger(__name__)


class S2Validator:
    """
    S2 — Stage 2 Validator

    Reads shared state (specifically A2 generated content) and runs
    content-level checks against the active rule pack. The pipeline
    proceeds to render `study_guide.docx` only when S2 returns a status
    of `pass` or `pass_with_warnings`.
    """

    def __init__(self, shared_state_path: str):
        self.shared_state_path = shared_state_path

    def run(self) -> S2ValidationReport:
        """Execute all S2 validation checks and return a typed S2ValidationReport."""

        logger.info("[S2] Loading shared state for content validation...")
        with open(self.shared_state_path) as f:
            shared_state = json.load(f)

        run_id = shared_state.get("run_id", "unknown")

        # ── Early exit: A2 not ready ─────────────────────────────────────
        a2_output = shared_state.get("agent_outputs", {}).get("A2", {}) or {}
        if not a2_output or a2_output.get("status") not in ("complete", "partial"):
            early_field = "a2_output.status" if a2_output else "a2_output"
            issue = ValidationIssue.model_validate({
                "field": early_field,
                "expected": "'complete' or 'partial'",
                "found": a2_output.get("status", "missing") if a2_output else "missing",
                "severity": "blocker",
                "message": "A2 must complete before S2 validation.",
                "rule_source": "pipeline",
            })
            return S2ValidationReport(
                status=S2Status.blocker,
                run_id=run_id,
                message="A2 output not found or incomplete. Cannot validate.",
                issues=[issue],
                blockers=1,
                criticals=0,
                warnings=0,
                infos=0,
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
            issue = ValidationIssue.model_validate({
                "field": "rule_pack",
                "expected": "valid rule family",
                "found": repr(rule_family),
                "severity": "blocker",
                "message": f"Rule pack not found for '{rule_family}'.",
                "rule_source": "rule_pack_config.rule_packs",
            })
            return S2ValidationReport(
                status=S2Status.blocker,
                run_id=run_id,
                message=f"Could not resolve rule pack for family '{rule_family}'.",
                issues=[issue],
                blockers=1,
                criticals=0,
                warnings=0,
                infos=0,
            )

        # ── Run all checks ───────────────────────────────────────────────
        logger.info(
            "[S2] Validating A2 content against rule pack: %s %s",
            rule_pack["family"],
            rule_pack["version"],
        )

        sections: list[dict] = a2_output.get("sections", []) or []
        raw_issues: list[dict] = []

        logger.info("[S2] Checking A2 completeness...")
        raw_issues.extend(check_a2_completeness(a2_output))

        logger.info("[S2] Checking section content...")
        raw_issues.extend(check_section_non_empty(sections))

        logger.info("[S2] Checking knowledge-check structure...")
        raw_issues.extend(check_kc_structure(sections, rule_pack))
        raw_issues.extend(check_kc_distractor_rationales(sections, rule_pack))
        raw_issues.extend(check_kc_placement(sections, rule_pack))

        logger.info("[S2] Checking compliance_elements...")
        raw_issues.extend(check_forbidden_phrases(sections, rule_pack))
        raw_issues.extend(check_required_behaviors(sections, rule_pack))
        raw_issues.extend(check_voice_pronouns(sections, rule_pack))
        raw_issues.extend(check_regulatory_mode(rule_pack))

        logger.info("[S2] Checking content_rules...")
        raw_issues.extend(check_intro_section(sections, rule_pack))
        raw_issues.extend(check_los_in_first_section(sections, rule_pack))
        raw_issues.extend(check_summary_section(sections, rule_pack))
        raw_issues.extend(check_callouts_per_section(sections, rule_pack))
        raw_issues.extend(check_examples_per_section(sections, rule_pack))
        raw_issues.extend(check_no_duplicate_headings(sections, rule_pack))
        raw_issues.extend(check_course_word_count_bands(a2_output, rule_pack))

        logger.info("[S2] Checking pacing & Lectora limits...")
        raw_issues.extend(check_lectora_page_limits(sections, rule_pack))

        # Word count: doc-bounds + conditional deviation live inside
        # check_word_count_against_doc_bounds (deviation runs only when that gate returns no issues).
        logger.info("[S2] Checking A2 output against document generation bounds...")
        raw_issues.extend(check_word_count_against_doc_bounds(a2_output, shared_state, rule_pack))

        logger.info("[S2] Checking LO coverage in generated content...")
        raw_issues.extend(check_lo_coverage(sections, shared_state, rule_pack))

        all_issues: list[ValidationIssue] = [
            ValidationIssue.model_validate(i) for i in raw_issues
        ]

        # ── Tally results ────────────────────────────────────────────────
        blockers  = [i for i in all_issues if i.severity == "blocker"]
        criticals = [i for i in all_issues if i.severity == "critical"]
        warnings  = [i for i in all_issues if i.severity == "warning"]
        infos     = [i for i in all_issues if i.severity == "info"]

        if blockers:
            status = S2Status.blocked
        elif criticals or warnings:
            status = S2Status.pass_with_warnings
        else:
            status = S2Status.pass_

        # ── Print report ─────────────────────────────────────────────────
        logger.info("[S2] Validation complete: %s", status.upper())
        logger.info(
            "     Blockers: %s  |  Criticals: %s  |  Warnings: %s  |  Info: %s",
            len(blockers),
            len(criticals),
            len(warnings),
            len(infos),
        )

        if blockers:
            logger.warning("  BLOCKERS (study_guide.docx will NOT be built):")
            for b in blockers:
                logger.warning("    [BLOCKER] %s: %s", b.field, b.message)
                logger.warning("              Rule: %s", b.rule_source)
                if b.failure_reason:
                    logger.warning("              Why: %s", b.failure_reason)
                if b.remediation:
                    logger.warning("              What to do: %s", b.remediation)

        if criticals:
            logger.warning("  CRITICALS (mandatory review before publishing):")
            for c in criticals:
                logger.warning("    [CRITICAL] %s: %s", c.field, c.message)
                logger.warning("               Rule: %s", c.rule_source)
                if c.failure_reason:
                    logger.warning("               Why: %s", c.failure_reason)
                if c.remediation:
                    logger.warning("               What to do: %s", c.remediation)

        if warnings:
            logger.info("  WARNINGS (review recommended):")
            for w in warnings:
                logger.info("    [WARNING] %s: %s", w.field, w.message)
                logger.info("              Rule: %s", w.rule_source)

        if infos:
            logger.info("  INFO:")
            for i in infos:
                logger.info("    [INFO] %s: %s", i.field, i.message)

        # ── Persist to shared state + sidecar ────────────────────────────
        output_dir = Path(self.shared_state_path).expanduser().resolve().parent
        report_path = output_dir / "s2_validation.json"

        blocked_msg = None
        if blockers:
            blocked_msg = (
                f"{len(blockers)} blocker(s): study_guide.docx will not be built. "
                "Each blocker issue includes failure_reason (why) and remediation (what to do)."
            )
        elif criticals:
            blocked_msg = (
                f"{len(criticals)} critical(s): mandatory review required before publishing."
            )

        report = S2ValidationReport(
            status=status,
            run_id=run_id,
            issues=all_issues,
            blockers=len(blockers),
            criticals=len(criticals),
            warnings=len(warnings),
            infos=len(infos),
            report_path=str(report_path.resolve()),
            message=blocked_msg,
        )

        validation_dict = report.model_dump(mode="json")
        validation_dict["timestamp"] = datetime.now(timezone.utc).isoformat()
        validation_dict["rule_pack_used"] = f"{rule_pack['family']} {rule_pack['version']}"

        shared_state["s2_validation"] = validation_dict
        shared_state["status"] = "S2_blocked" if status == S2Status.blocked else "S2_validated"

        with open(self.shared_state_path, "w") as f:
            json.dump(shared_state, f, indent=2, default=str)

        with open(report_path, "w") as f:
            json.dump(validation_dict, f, indent=2, default=str)

        logger.info("[S2] Report saved -> %s", report_path)
        return report
