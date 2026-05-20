"""
S2 — Word-count, pacing, and generation-bounds validation checks.

Validates:
  - Lectora per-page word limits
  - A2 word count vs TO outline target (``error_tolerance`` / deviation)
  - A2 word count vs rule-pack ``course_word_count_bands``
  - A2 word count vs doc bounds (Path A: ``min_gen``/``max_gen`` band; A2 may exceed
    ``max_gen`` up to full TO as ``above_max_within_to`` critical; **block** if A2 > TO).
    Path B: direct full TO. See ``DOC_BOUNDS_WORD_COUNT.md``.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# --- Doc-bounds factors (keep in sync with ``DOC_BOUNDS_WORD_COUNT.md``) ------------
# Source is “rich” when total_doc > RICH_SOURCE_FACTOR × TO — then use direct-TO path.
RICH_SOURCE_FACTOR = 1.4
# Path A: min floor; max_gen is soft cap — A2 may go up to TO (critical) but not past TO.
MIN_GEN_FACTOR = 0.5
MAX_GEN_FACTOR = 0.8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _section_total_words(sec: dict) -> int:
    """Use stored word_count if present, otherwise approximate from blocks."""
    wc = sec.get("word_count")
    if isinstance(wc, int) and wc > 0:
        return wc

    if isinstance(wc, str):
        digits = re.sub(r"[^\d]", "", wc)
        if digits:
            return int(digits)

    total = 0
    for block in sec.get("body_paragraphs", []) or []:
        text = block.get("content") or ""
        if isinstance(text, str):
            total += len(text.split())

        for item in block.get("items", []) or []:
            if isinstance(item, str):
                total += len(item.split())

    return total


def _parse_to_word_count(raw: str | int | None) -> int | None:
    """Parse TO totals ``word_count`` into an int.

    Handles formats like ``7345 words``, bare digits, or positive int. Returns None if invalid.
    """
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if not isinstance(raw, str):
        return None
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits and int(digits) > 0 else None


def _doc_size_label_vs_to(total_doc: int, to_target: int) -> str:
    """Short phrase for messages: how source size compares to TO (timed-outline total)."""
    if total_doc < to_target:
        return "below"
    if total_doc > to_target:
        return "above"
    return "equal to"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_lectora_page_limits(sections: list[dict], rule_pack: dict) -> list[dict]:
    """Flag sections that grossly exceed Lectora per-page word limits."""
    issues: list[dict] = []
    lectora = rule_pack.get("lectora_constraints", {}) or {}
    max_per_page = lectora.get("max_words_per_page")
    if not max_per_page:
        return issues

    threshold = int(max_per_page) * 10  # ~10 pages worth
    for sec in sections:
        wc = _section_total_words(sec)
        if wc > threshold:
            pages_approx = wc // int(max_per_page)
            issues.append(
                {
                    "field": f"section.{sec.get('section_id') or sec.get('heading')}.word_count",
                    "expected": f"<= {threshold}",
                    "found": wc,
                    "severity": "info",
                    "message": (
                        f"Section '{sec.get('heading')}' has {wc} words "
                        f"(about {pages_approx} Lectora pages), which may be too long to display properly. "
                        "Consider splitting it into smaller subsections."
                    ),
                    "rule_source": "lectora_constraints.max_words_per_page",
                    "failure_reason": (
                        f"Section '{sec.get('heading')}' has {wc} words, which is more than the recommended "
                        f"limit of {threshold} words (about 10 Lectora pages). "
                        f"This would translate to approximately {pages_approx} pages in the published course."
                    ),
                    "remediation": (
                        "Split this section into two or more smaller subsections so each part fits "
                        "comfortably within the Lectora page layout."
                    ),
                }
            )
    return issues


def check_course_word_count_bands(a2_output: dict, rule_pack: dict) -> list[dict]:
    """``content_rules.course_word_count_bands`` — flag if total falls outside min/max."""
    issues: list[dict] = []
    bands = (rule_pack.get("content_rules", {}) or {}).get("course_word_count_bands")
    if not isinstance(bands, dict) or not bands:
        return issues

    total = (a2_output.get("stats") or {}).get("total_words")
    if not isinstance(total, int) or total <= 0:
        return issues

    lo = bands.get("min")
    hi = bands.get("max")
    if isinstance(lo, int) and total < lo:
        short_by = lo - total
        issues.append(
            {
                "field": "a2_output.stats.total_words",
                "expected": f">= {lo}",
                "found": total,
                "severity": "warning",
                "message": (
                    f"The course has {total} words in total, which is below the required minimum of {lo} words. "
                    f"About {short_by} more words are needed."
                ),
                "rule_source": "content_rules.course_word_count_bands",
                "failure_reason": (
                    f"The course generated {total} words, but the rule pack requires a minimum of {lo} words. "
                    f"The course is {short_by} words too short."
                ),
                "remediation": (
                    f"Add about {short_by} words of relevant content to bring the course up to the minimum length."
                ),
            }
        )
    if isinstance(hi, int) and total > hi:
        over_by = total - hi
        issues.append(
            {
                "field": "a2_output.stats.total_words",
                "expected": f"<= {hi}",
                "found": total,
                "severity": "warning",
                "message": (
                    f"The course has {total} words in total, which exceeds the maximum allowed length of {hi} words "
                    f"by {over_by} words. Some content needs to be trimmed."
                ),
                "rule_source": "content_rules.course_word_count_bands",
                "failure_reason": (
                    f"The course generated {total} words, but the rule pack allows a maximum of {hi} words. "
                    f"The course is {over_by} words too long."
                ),
                "remediation": (
                    f"Remove about {over_by} words from the course to bring it within the maximum allowed length."
                ),
            }
        )
    return issues


def check_word_count_target(
    a2_output: dict, shared_state: dict, rule_pack: dict
) -> list[dict]:
    """Compare A2 total to section-map TO within ``error_tolerance.word_count_tolerance_percent``.

    Target: ``shared_state["agent_outputs"]["section_map"]["to_totals"]["word_count"]``.

    Escalation:
        delta_pct ≤ tol_pct              → no issue
        tol_pct < delta_pct ≤ 3×tol_pct  → warning
        delta_pct > 3×tol_pct            → blocker
    """
    issues: list[dict] = []
    tolerance_cfg = rule_pack.get("error_tolerance", {}) or {}
    tol_pct = tolerance_cfg.get("word_count_tolerance_percent")
    if not tol_pct:
        return issues

    to_totals = (
        shared_state.get("agent_outputs", {})
        .get("section_map", {})
        .get("to_totals", {})
    )
    to_target = _parse_to_word_count(to_totals.get("word_count"))
    if to_target is None:
        return issues

    a2_total = a2_output.get("stats", {}).get("total_words")
    if not isinstance(a2_total, int) or a2_total <= 0:
        return issues

    delta_pct = abs(a2_total - to_target) / to_target * 100.0
    if delta_pct <= float(tol_pct):
        return issues

    severity = "blocker" if delta_pct > 3.0 * float(tol_pct) else "warning"
    tol_f = float(tol_pct)
    issues.append(
        {
            "field": "a2_output.stats.total_words",
            "expected": f"within ±{tol_pct}% of TO target ({to_target})",
            "found": f"{a2_total} ({delta_pct:.1f}% off)",
            "severity": severity,
            "message": (
                f"A2 generated {a2_total} words vs TO target {to_target} "
                f"({delta_pct:.1f}% deviation, allowed ±{tol_pct}%)."
                + (
                    "  This exceeds 3× the tolerance and indicates a systemic "
                    "generation failure — A2 should be retried."
                    if severity == "blocker"
                    else ""
                )
            ),
            "rule_source": "error_tolerance.word_count_tolerance_percent",
            "failure_reason": (
                f"Section-map TO target is {to_target} words (from to_totals.word_count); "
                f"A2 total is {a2_total}. Relative gap is {delta_pct:.1f}% vs allowed ±{tol_pct}% "
                f"(rule_pack error_tolerance.word_count_tolerance_percent)."
                + (
                    f"  Deviation exceeds 3× tolerance ({3 * tol_f:.1f}%)."
                    if severity == "blocker"
                    else ""
                )
            ),
            "remediation": (
                "Re-run A2 with explicit length targets aligned to the timed-outline total; "
                "verify section_map / TO word_count matches extracted_inputs."
                if severity == "blocker"
                else (
                    f"Adjust generation prompts or edit sections so total moves within ±{tol_pct}% "
                    f"of {to_target} words (~{abs(a2_total - to_target)} words difference)."
                )
            ),
        }
    )
    return issues


def _difficulty_adjusted_to_target(to_raw: int, rule_pack: dict) -> int:
    """
    Apply the difficulty multiplier to the raw TO word count.

    Formula: base 9,000 words/CE hr × difficulty factor
      basic        1.0 ×  →  9,000 words/CE hr (no change)
      intermediate 1.25 × → 11,250 words/CE hr
      advanced     1.5 ×  → 13,500 words/CE hr

    The multiplier is stored in ``content_rules.difficulty_multiplier`` by the
    per-difficulty overlay in ``insurance_ce_difficulty.py``.
    """
    mult = float(
        (rule_pack.get("content_rules") or {}).get("difficulty_multiplier") or 1.0
    )
    return round(to_raw * mult)


def check_word_count_against_doc_bounds(
    a2_output: dict,
    shared_state: dict,
    _rule_pack: dict,
) -> list[dict]:
    """
    Doc-bounds gate for A2 total vs Timed Outline (TO) and source study-guide size.

    **Inputs** (``shared_state["extracted_inputs"]``):
      - ``to_outline_total_word_count`` → TO base word count (at 9,000 words/CE hr)
      - ``total_doc_word_count`` → source document words
      - ``a2_output["stats"]["total_words"]`` → generated total

    The TO base word count is scaled by the difficulty multiplier before comparison:
      basic 1.0×, intermediate 1.25×, advanced 1.5×.

    **Skip** if any of TO, source, or A2 total is missing or non-positive.

    **Branch A — Bounds path** when ``total_doc <= int(RICH_SOURCE_FACTOR × adjusted_TO)``:
      ``min_gen``, ``max_gen`` from module constants. Outcomes:
      ``A2 < min_gen`` → blocker; ``min_gen ≤ A2 ≤ max_gen`` → critical (in band);
      ``max_gen < A2 ≤ TO`` → critical (above soft ceiling but not over TO); ``A2 > TO`` → blocker.

    **Branch B — Direct TO** when ``total_doc > int(RICH_SOURCE_FACTOR × adjusted_TO)``:
      Compare A2 to full ``to_target`` (under → warning, over → blocker).

    If Branch B yields **no** issues from this function, append ``check_word_count_target``
    (deviation vs section-map TO).

    See ``DOC_BOUNDS_WORD_COUNT.md``.
    """
    issues: list[dict] = []

    extracted = shared_state.get("extracted_inputs") or {}
    to_raw = int(extracted.get("to_outline_total_word_count") or 0)
    # Apply difficulty multiplier: basic 1.0×, intermediate 1.25×, advanced 1.5×
    to_target = _difficulty_adjusted_to_target(to_raw, _rule_pack)
    mult = float(
        (_rule_pack.get("content_rules") or {}).get("difficulty_multiplier") or 1.0
    )
    if to_target != to_raw:
        logger.info(
            "[S2] TO word count adjusted for difficulty (%.2f×): %s → %s",
            mult, to_raw, to_target,
        )
    total_doc = int(extracted.get("total_doc_word_count") or 0)
    a2_total = (a2_output.get("stats") or {}).get("total_words")

    if (
        not isinstance(a2_total, int)
        or a2_total <= 0
        or to_target <= 0
        or total_doc <= 0
    ):
        return issues

    doc_pct_of_to = total_doc / to_target * 100
    a2_pct_of_to = a2_total / to_target * 100
    rich_threshold = int(to_target * RICH_SOURCE_FACTOR)
    min_gen = int(to_target * MIN_GEN_FACTOR)
    max_gen = int(to_target * MAX_GEN_FACTOR)
    doc_label = _doc_size_label_vs_to(total_doc, to_target)

    # ── Branch A: source not "rich" — enforce min_gen / max_gen band ─────────
    if total_doc <= rich_threshold:
        if a2_total < min_gen:
            shortfall = min_gen - a2_total
            issues.append(
                {
                    "field": "a2_output.stats.total_words",
                    "expected": f">= {min_gen}  ({int(MIN_GEN_FACTOR * 100)}% of TO {to_target})",
                    "found": f"{a2_total} words ({a2_pct_of_to:.1f}% of TO target)",
                    "severity": "blocker",
                    "message": (
                        f"The course only generated {a2_total} words, which is {shortfall} words below the minimum "
                        f"required length of {min_gen} words "
                        f"({int(MIN_GEN_FACTOR * 100)}% of the {to_target}-word Timed Outline target). "
                        "The content needs to be regenerated."
                    ),
                    "rule_source": "doc_bounds.min_gen",
                    "failure_reason": (
                        f"The source document ({total_doc} words) is not much larger than the Timed Outline target "
                        f"({to_target} words), so the allowed generation range is {min_gen}\u2013{max_gen} words. "
                        f"The generated course ({a2_total} words) is {shortfall} words below the minimum."
                    ),
                    "remediation": (
                        f"The content needs {shortfall} more words to reach the minimum of {min_gen} words. "
                        "Please regenerate with stronger length instructions and ensure no sections were skipped."
                    ),
                }
            )
            return issues

        if a2_total <= max_gen:
            above_min = a2_total - min_gen
            below_max = max_gen - a2_total
            issues.append(
                {
                    "field": "a2_output.stats.total_words",
                    "expected": (
                        f"within {min_gen}\u2013{max_gen} words "
                        f"({int(MIN_GEN_FACTOR * 100)}%\u2013{int(MAX_GEN_FACTOR * 100)}% of TO {to_target})"
                    ),
                    "found": f"{a2_total} words ({a2_pct_of_to:.1f}% of TO target)",
                    "severity": "critical",
                    "message": (
                        f"The course generated {a2_total} words, which is within the acceptable range of "
                        f"{min_gen}\u2013{max_gen} words. However, because the source document ({total_doc} words) "
                        f"is not much larger than the Timed Outline target ({to_target} words), "
                        "a mandatory human review is required before this course can be published."
                    ),
                    "rule_source": "doc_bounds.in_bounds_band",
                    "failure_reason": (
                        f"The source document ({total_doc} words) is {doc_label} the Timed Outline target "
                        f"({to_target} words), so the allowed generation range is {min_gen}\u2013{max_gen} words. "
                        f"The generated course ({a2_total} words) is inside this range "
                        f"({above_min} words above the minimum, {below_max} words below the maximum)."
                    ),
                    "remediation": (
                        "A mandatory review is required before publishing. "
                        f"For better results, consider providing a larger source document "
                        f"(at least {rich_threshold} words) so the full Timed Outline target can be used directly."
                    ),
                }
            )
            return issues

        # Remaining: A2 > max_gen — either still within TO (critical) or over TO (blocker).
        if a2_total > to_target:
            above_to = a2_total - to_target
            issues.append(
                {
                    "field": "a2_output.stats.total_words",
                    "expected": f"<= {to_target}  (TO \u2014 bounds path; cannot exceed timed-outline total)",
                    "found": f"{a2_total} words ({a2_pct_of_to:.1f}% of TO target)",
                    "severity": "blocker",
                    "message": (
                        f"The course generated {a2_total} words, which is {above_to} words above the Timed Outline "
                        f"target of {to_target} words. The content exceeds the agreed length and must be trimmed or regenerated."
                    ),
                    "rule_source": "doc_bounds.bounds_path_over_to",
                    "failure_reason": (
                        f"The source document ({total_doc} words) is not much larger than the Timed Outline target "
                        f"({to_target} words), so generated content must not exceed the full TO total. "
                        f"The generated course ({a2_total} words) is {above_to} words over the TO target."
                    ),
                    "remediation": (
                        f"Trim at least {above_to} words so the course stays within the Timed Outline total of {to_target} words."
                    ),
                }
            )
            return issues

        above_max = a2_total - max_gen
        below_to = to_target - a2_total
        issues.append(
            {
                "field": "a2_output.stats.total_words",
                "expected": f"<= {to_target} words (review required above {max_gen})",
                "found": f"{a2_total} words ({a2_pct_of_to:.1f}% of TO target)",
                "severity": "critical",
                "message": (
                    f"The course generated {a2_total} words, which is above the soft upper limit of {max_gen} words "
                    f"({int(MAX_GEN_FACTOR * 100)}% of TO) but still under the Timed Outline total of {to_target} words. "
                    "A mandatory review is required before publishing."
                ),
                "rule_source": "doc_bounds.above_max_within_to",
                "failure_reason": (
                    f"The generated course ({a2_total} words) exceeds the 80% soft ceiling ({max_gen} words) "
                    f"but has not crossed the full Timed Outline target ({to_target} words). "
                    f"It is {above_max} words above the soft limit and {below_to} words below the TO total."
                ),
                "remediation": (
                    "Mandatory review before publishing. Optionally trim toward the soft maximum if stakeholders "
                    "want the course closer to the intended length band."
                ),
            }
        )
        return issues

    # ── Branch B: rich source — compare A2 directly to full TO ─────────────────
    if a2_total < to_target:
        shortfall = to_target - a2_total
        issues.append(
            {
                "field": "a2_output.stats.total_words",
                "expected": f">= {to_target}  (Timed Outline target)",
                "found": f"{a2_total} words ({a2_pct_of_to:.1f}% of TO target)",
                "severity": "warning",
                "message": (
                    f"The course generated {a2_total} words, but the Timed Outline target is {to_target} words. "
                    f"The content is {shortfall} words short of the agreed length. "
                    "Please review before publishing."
                ),
                "rule_source": "doc_bounds.direct_to_under",
                "failure_reason": (
                    f"The source document ({total_doc} words) is substantially larger than the Timed Outline "
                    f"target ({to_target} words), so the generated content is compared directly to the full target. "
                    f"The generated course ({a2_total} words) is {shortfall} words short."
                ),
                "remediation": (
                    f"Add {shortfall} words to match the Timed Outline target of {to_target} words. "
                    "This can be accepted with stakeholder sign-off if the gap is minor."
                ),
            }
        )
    elif a2_total > to_target:
        above_to = a2_total - to_target
        issues.append(
            {
                "field": "a2_output.stats.total_words",
                "expected": f"<= {to_target}  (Timed Outline target)",
                "found": f"{a2_total} words ({a2_pct_of_to:.1f}% of TO target)",
                "severity": "blocker",
                "message": (
                    f"The course generated {a2_total} words, which is {above_to} words above the Timed Outline "
                    f"target of {to_target} words. The content exceeds the agreed length and must be trimmed."
                ),
                "rule_source": "doc_bounds.direct_to_over",
                "failure_reason": (
                    f"The source document ({total_doc} words) is substantially larger than the Timed Outline "
                    f"target ({to_target} words), so the generated content is compared directly to the full target. "
                    f"The generated course ({a2_total} words) is {above_to} words over the TO target."
                ),
                "remediation": (
                    f"Trim {above_to} words from the course to align with the Timed Outline target of {to_target} words."
                ),
            }
        )

    if not issues:
        logger.info("[S2] Doc-bounds passed — running deviation check...")
        issues.extend(check_word_count_target(a2_output, shared_state, _rule_pack))

    return issues
