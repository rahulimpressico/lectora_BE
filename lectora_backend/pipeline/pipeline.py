"""
Multi-Agent Course Authoring Pipeline
--------------------------------------
Flow:
  (A0 → A1 → S1) repeated up to MAX_A0_A1_S1_CYCLES if S1 is blocked
      → Section Mapper
      → (A2 → S2) repeated up to MAX_A2_S2_CYCLES if S2 is blocked
      → study_guide.docx

Gates:
  - S1 blocked  → full A0 → A1 → S1 retry (fresh shared state each cycle)
  - A1 failure  → stops the loop immediately (S1 is not run for that cycle)
  - Max 3 full A0/A1/S1 cycles before hard stop
  - A2 writes generated_content.json only; study_guide.docx is rendered
    ONLY after S2 returns pass or pass_with_warnings
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Repo root (parent of `lectora_backend/`) so running this file directly works
# without `PYTHONPATH=.` or `python -m lectora_backend.pipeline.pipeline`.
_root = str(Path(__file__).resolve().parents[2])
if _root not in sys.path:
    sys.path.insert(0, _root)

from lectora_backend.core.logging_config import configure_logging

configure_logging()

from lectora_backend.pipeline.agent.a0_request_synthesizer import A0RequestSynthesizer
from lectora_backend.pipeline.agent.a1_outline_interpreter import run as a1_run
from lectora_backend.pipeline.agent.a2_content_generator import (
    A2ContentGenerator,
    render_study_guide_from_state,
)
from lectora_backend.pipeline.agent.s1_validator import S1Validator
from lectora_backend.pipeline.agent.s2_validator import S2Validator
from lectora_backend.pipeline.agent.section_mapper import run as section_mapper_run
from lectora_backend.pipeline.agent.kc_planner import run as kc_planner_run
from lectora_backend.pipeline.models import (
    A0Result,
    A1Output,
    A2Output,
    S1ValidationReport,
    S2ValidationReport,  # noqa: F401 — used in type hints and _format helpers
)
from lectora_backend.pipeline.models.validation import S1Status, S2Status
from lectora_backend.pipeline.shared_llm_config.tracer import set_doc_name, set_run_id
from lectora_backend.models.constants import MAX_A0_A1_S1_CYCLES, MAX_A2_S2_CYCLES


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent.resolve()

SHARED_STATE_DIR = str(_HERE / "shared_state")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _separator(label: str) -> None:
    logger.info("=" * 70)
    logger.info("  %s", label)
    logger.info("=" * 70)


def _format_s1_feedback(report: S1ValidationReport) -> str:
    """Render S1 blockers and warnings as a compact text block for A1 retry."""
    lines: list[str] = []
    for issue in report.issues:
        if issue.severity == "blocker":
            lines.append(f"[BLOCKER] {issue.field}: {issue.message} (rule: {issue.rule_source})")
        elif issue.severity == "warning":
            lines.append(f"[WARNING] {issue.field}: {issue.message} (rule: {issue.rule_source})")
    return "\n".join(lines)


def _persist_course_difficulty(shared_state_path: str, difficulty: str) -> None:
    """Write ``course_difficulty`` into shared_state.json for downstream agents."""
    path = Path(shared_state_path).expanduser().resolve()
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    state["course_difficulty"] = difficulty.strip().lower()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def _format_s2_feedback(report: S2ValidationReport) -> str:
    """Render an S2 report into a compact text block to feed back into A2."""
    lines: list[str] = []
    if report.blockers:
        lines.append("Blockers (must fix):")
        for issue in report.issues:
            if issue.severity == "blocker":
                lines.append(
                    f"  - [{issue.field}] {issue.message} (rule: {issue.rule_source})"
                )
    if report.criticals:
        lines.append("Critical issues (must address):")
        for issue in report.issues:
            if issue.severity == "critical":
                lines.append(
                    f"  - [{issue.field}] {issue.message} (rule: {issue.rule_source})"
                )
    if report.warnings:
        lines.append("Warnings (please address):")
        for issue in report.issues:
            if issue.severity == "warning":
                lines.append(
                    f"  - [{issue.field}] {issue.message} (rule: {issue.rule_source})"
                )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    docx_path: str,
    to_outline_doc_path: str | None = None,
    course_difficulty: str = "intermediate",
    extra_docx_paths: list[str] | None = None,
) -> None:
    """
    Run the full multi-agent pipeline for one or more source documents.

    Args:
        docx_path:           Path to the primary source .docx course document.
        to_outline_doc_path: Path to the Timed Outline .docx.
            - Scenario 1 (provided): TO is used as the primary course structure.
            - Scenario 2 (omitted): A0 generates a complete TO from the source
              content via LLM. Rule packs with ``require_timed_outline`` will fail
              S1 in this scenario.
        course_difficulty:   ``basic``, ``intermediate``, or ``advanced`` — merged into
            the Insurance CE rule pack (default ``intermediate``).
        extra_docx_paths:    Optional list of additional source .docx files. Their
            content is merged with the primary document for classification and,
            when no TO is provided, for TO generation.
    """
    course_difficulty = (course_difficulty or "intermediate").strip().lower()
    extra_docx_paths = extra_docx_paths or []

    start = datetime.now(timezone.utc)
    set_doc_name(Path(docx_path).stem)

    _separator("PIPELINE START")
    logger.info("Document   : %s", docx_path)
    if extra_docx_paths:
        for i, p in enumerate(extra_docx_paths, 1):
            logger.info("Extra doc %s: %s", i, p)
    logger.info("Difficulty : %s", course_difficulty)
    logger.info(
        "TO doc   : %s",
        to_outline_doc_path if to_outline_doc_path else "(none — TO will be generated from source)",
    )
    logger.info("Started  : %s", start.isoformat())

    # ── A0 → A1 → S1  (up to MAX_A0_A1_S1_CYCLES full cycles) ────────────
    _separator(f"A0 → A1 → S1 (up to {MAX_A0_A1_S1_CYCLES} full cycles)")

    a0: A0Result | None = None
    a1_final: A1Output | None = None
    s1_result: S1ValidationReport | None = None
    feedback: dict | None = None
    shared_state_path = ""
    run_id = ""

    for cycle in range(1, MAX_A0_A1_S1_CYCLES + 1):
        logger.info(">>> Cycle %s/%s — A0 → A1 → S1", cycle, MAX_A0_A1_S1_CYCLES)

        _separator(f"A0 — Request Synthesizer (cycle {cycle})")
        a0 = A0RequestSynthesizer(
            docx_path=docx_path,
            output_dir=SHARED_STATE_DIR,
            to_outline_doc_path=to_outline_doc_path,
            extra_docx_paths=extra_docx_paths or None,
            course_difficulty=course_difficulty,
        ).run()
        shared_state_path = a0.output_files.shared_state
        _persist_course_difficulty(shared_state_path, course_difficulty)
        run_id = a0.request_spec.run_id
        set_run_id(run_id)
        logger.info("Run ID    : %s", run_id)
        logger.info("State file: %s", shared_state_path)

        _separator(f"A1 — Timed Outline Interpreter (cycle {cycle})")
        a1_final = a1_run(
            shared_state_path=shared_state_path,
            docx_path=docx_path,
            feedback=feedback,
        )

        if a1_final.status != "complete":
            logger.error("A1 failed: %s", a1_final.error)
            break

        spec = a1_final.course_spec
        section_count = len(spec.sections) if spec else 0
        word_count = sum((s.word_count or 0) for s in spec.sections) if spec else 0
        logger.info("Sections: %s", section_count)
        logger.info("Words   : %s (sections summed)", word_count)

        _separator(f"S1 — Stage 1 Validator (cycle {cycle})")
        s1_result = S1Validator(shared_state_path=shared_state_path).run()
        logger.info("S1 status : %s", s1_result.status)
        logger.info("Blockers  : %s", s1_result.blockers)
        logger.info("Warnings  : %s", s1_result.warnings)

        if s1_result.status not in (S1Status.blocked, S1Status.blocker):
            logger.info("S1 passed — advancing to Section Mapper.")
            break

        logger.warning("S1 blocked — next cycle will re-run A0 → A1 → S1.")
        feedback = {
            "validator_feedback": _format_s1_feedback(s1_result),
            "attempt": cycle,
        }

    if not s1_result or s1_result.status in (S1Status.blocked, S1Status.blocker):
        logger.error("Pipeline stopped: max retries reached; S1 still blocked.")
        return

    # ── Section Mapper ─────────────────────────────────────────────────────
    _separator("Section Mapper — TO outline → course_spec grouping")
    sm_result = section_mapper_run(shared_state_path=shared_state_path)
    logger.info("Sections mapped: %s", len(sm_result.get("enriched_sections", [])))

    # ── KC Planner ─────────────────────────────────────────────────────────
    _separator("KC Planner — determining Knowledge Check placement")
    kc_result = kc_planner_run(shared_state_path=shared_state_path)
    logger.info("KC scenario : %s", kc_result.get("scenario"))
    logger.info("KCs placed  : %s", kc_result.get("kc_count"))

    # ── A2 ↔ S2  (up to MAX_A2_S2_CYCLES; docx deferred until S2 passes) ─
    _separator(f"A2 ↔ S2 (up to {MAX_A2_S2_CYCLES} cycles)")

    a2: A2Output | None = None
    s2_result: S2ValidationReport | None = None
    a2_feedback: str | None = None

    for cycle in range(1, MAX_A2_S2_CYCLES + 1):
        logger.info(">>> A2/S2 cycle %s/%s", cycle, MAX_A2_S2_CYCLES)

        _separator(f"A2 — Content Generator (cycle {cycle})")
        a2 = A2ContentGenerator(
            shared_state_path=shared_state_path,
            docx_path=docx_path,
            render_docx=False,
            feedback=a2_feedback,
            course_difficulty=course_difficulty,
        ).run()
        logger.info("A2 status : %s", a2.status)
        logger.info("Generated : %s", a2.stats.generated)
        logger.info("Skipped   : %s", a2.stats.skipped)
        logger.info("Failed    : %s", a2.stats.failed)
        logger.info("Words     : %s", a2.stats.total_words)

        _separator(f"S2 — Stage 2 Validator (cycle {cycle})")
        s2_result = S2Validator(shared_state_path=shared_state_path).run()
        logger.info("S2 status : %s", s2_result.status)
        logger.info("Blockers  : %s", s2_result.blockers)
        logger.info("Warnings  : %s", s2_result.warnings)

        if s2_result.status not in (S2Status.blocked, S2Status.blocker):
            logger.info("S2 passed — content cleared for DOCX rendering.")
            break

        logger.warning(
            "S2 blocked — regenerating A2 with feedback (cycle %s/%s).",
            cycle,
            MAX_A2_S2_CYCLES,
        )
        a2_feedback = _format_s2_feedback(s2_result)

    # ── S2 hard stop ───────────────────────────────────────────────────────
    if not s2_result or s2_result.status in (S2Status.blocked, S2Status.blocker):
        logger.error(
            "Pipeline stopped: S2 still blocked after %s cycle(s); "
            "study_guide.docx will NOT be built.",
            MAX_A2_S2_CYCLES,
        )
        _separator("SUMMARY")
        logger.info("Run ID : %s", run_id)
        logger.info("A1     : %s", a1_final.status if a1_final else "n/a")
        logger.info("A2     : %s", a2.status if a2 else "n/a")
        logger.info("S2     : %s", s2_result.status if s2_result else "n/a")
        logger.info("Output : %s/", SHARED_STATE_DIR)
        return

    # ── Render study_guide.docx ────────────────────────────────────────────
    _separator("Rendering study_guide.docx (S2 passed)")
    docx_final = render_study_guide_from_state(shared_state_path=shared_state_path)
    logger.info("Study guide -> %s", docx_final)

    _separator("SUMMARY")
    logger.info("Run ID : %s", run_id)
    logger.info("A1     : %s", a1_final.status if a1_final else "n/a")
    logger.info("A2     : %s", a2.status if a2 else "n/a")
    logger.info("S2     : %s", s2_result.status)
    logger.info("DOCX   : %s", docx_final)
    logger.info("Output : %s/", SHARED_STATE_DIR)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _DOC_DIR = _HERE / "doc"

    run_pipeline(
        docx_path=str(_DOC_DIR / "146_SG_froikin_20250714_ACCEPTED.docx"),
        course_difficulty="intermediate",
        # Optional — omit or pass None to run without a timed-outline .docx
        # to_outline_doc_path=str(_DOC_DIR / "To_Outline_flood.docx"),
    )
