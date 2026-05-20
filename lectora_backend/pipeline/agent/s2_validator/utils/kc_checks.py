"""
S2 — Knowledge-check validation checks.

Validates every knowledge_check block in A2 generated sections against
kc_placement_rules and assessment_rules in the active rule pack.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TRUE_FALSE_HINT = ("true", "false")


def _iter_kc_blocks(sections: list[dict]):
    """Yield (section, block) for every knowledge_check block in A2 sections."""
    for sec in sections:
        for block in sec.get("body_paragraphs", []) or []:
            if block.get("type") == "knowledge_check":
                yield sec, block


# ---------------------------------------------------------------------------
# KC checks
# ---------------------------------------------------------------------------

def check_kc_structure(sections: list[dict], rule_pack: dict) -> list[dict]:
    """Every KC block must satisfy kc_placement_rules + assessment_rules constraints."""
    issues: list[dict] = []
    kc_rules = rule_pack.get("kc_placement_rules", {}) or {}
    assessment = rule_pack.get("assessment_rules", {}) or {}

    min_opts = kc_rules.get("min_answer_options")
    max_opts = kc_rules.get("max_answer_options")
    require_explanation = bool(kc_rules.get("require_explanation"))
    forbidden_types: list[str] = list(assessment.get("forbidden_question_types") or [])
    allow_true_false = assessment.get("allow_true_false", True)
    allow_aota = assessment.get("allow_all_of_the_above", True)

    for sec, block in _iter_kc_blocks(sections):
        sid = sec.get("section_id") or sec.get("heading") or "?"
        opts: list[str] = block.get("options") or []
        opt_count = len(opts)

        if min_opts is not None and opt_count < int(min_opts):
            issues.append({
                "field": f"section.{sid}.knowledge_check.options",
                "expected": f">= {min_opts}",
                "found": opt_count,
                "severity": "blocker",
                "message": (
                    f"The quiz question in '{sec.get('heading')}' only has {opt_count} answer option(s), "
                    f"but the rules require at least {min_opts}."
                ),
                "rule_source": "kc_placement_rules.min_answer_options",
            })
        if max_opts is not None and opt_count > int(max_opts):
            issues.append({
                "field": f"section.{sid}.knowledge_check.options",
                "expected": f"<= {max_opts}",
                "found": opt_count,
                "severity": "blocker",
                "message": (
                    f"The quiz question in '{sec.get('heading')}' has {opt_count} answer options, "
                    f"but the rules allow a maximum of {max_opts}."
                ),
                "rule_source": "kc_placement_rules.max_answer_options",
            })

        ca = (block.get("correct_answer") or "").strip().upper()
        max_letter = chr(ord("A") + max(opt_count - 1, 0)) if opt_count else ""
        if not ca or len(ca) != 1 or not ("A" <= ca <= max_letter):
            issues.append({
                "field": f"section.{sid}.knowledge_check.correct_answer",
                "expected": (
                    f"letter in A–{max_letter}" if opt_count else "single option letter"
                ),
                "found": block.get("correct_answer"),
                "severity": "blocker",
                "message": (
                    f"The correct answer for the quiz question in '{sec.get('heading')}' is missing or invalid. "
                    "It must point to one of the listed options (e.g. A, B, C, or D)."
                ),
                "rule_source": "assessment_rules",
            })

        if require_explanation and not (block.get("explanation") or "").strip():
            issues.append({
                "field": f"section.{sid}.knowledge_check.explanation",
                "expected": "non-empty",
                "found": "empty",
                "severity": "blocker",
                "message": (
                    f"The quiz question in '{sec.get('heading')}' is missing an explanation. "
                    "An explanation is required to help learners understand why the answer is correct."
                ),
                "rule_source": "kc_placement_rules.require_explanation",
            })

        # T/F detection (binary opts that look like True/False)
        if not allow_true_false and opt_count == 2:
            normalized = [o.split(")", 1)[-1].strip().lower() for o in opts]
            if all(any(h in n for h in _TRUE_FALSE_HINT) for n in normalized):
                issues.append({
                    "field": f"section.{sid}.knowledge_check.type",
                    "expected": "non-True/False question",
                    "found": "True/False",
                    "severity": "blocker",
                    "message": (
                        f"A True/False question was found in '{sec.get('heading')}', "
                        "but this course type does not allow True/False questions. "
                        "Please rewrite it as a multiple-choice question (A/B/C/D)."
                    ),
                    "rule_source": "assessment_rules.allow_true_false",
                })

        # All-of-the-above / None-of-the-above detection
        norm_opts_lower = [o.lower() for o in opts]
        if not allow_aota and any("all of the above" in o for o in norm_opts_lower):
            issues.append({
                "field": f"section.{sid}.knowledge_check.options",
                "expected": "no 'all of the above'",
                "found": "'all of the above' present",
                "severity": "blocker",
                "message": (
                    f"The quiz question in '{sec.get('heading')}' uses 'All of the above' as an answer option, "
                    "which is not allowed for this course type."
                ),
                "rule_source": "assessment_rules.allow_all_of_the_above",
            })

        for ftype in forbidden_types:
            ft = ftype.lower()
            if ft == "true_false" and not allow_true_false:
                continue
            if ft == "all_of_the_above" and not allow_aota:
                continue
            if ft == "none_of_the_above" and any("none of the above" in o for o in norm_opts_lower):
                issues.append({
                    "field": f"section.{sid}.knowledge_check.options",
                    "expected": "no 'none of the above'",
                    "found": "'none of the above' present",
                    "severity": "blocker",
                    "message": (
                        f"The quiz question in '{sec.get('heading')}' uses 'None of the above' as an answer option, "
                        "which is not allowed for this course type."
                    ),
                    "rule_source": "assessment_rules.forbidden_question_types",
                })
            if ft == "except_questions":
                stem = (block.get("question") or "").lower()
                if " except" in stem:
                    issues.append({
                        "field": f"section.{sid}.knowledge_check.question",
                        "expected": "no 'except' phrasing",
                        "found": "'except' in stem",
                        "severity": "warning",
                        "message": (
                            f"The quiz question in '{sec.get('heading')}' uses 'except' in its wording, "
                            "which is not allowed. Please rewrite the question without 'except'."
                        ),
                        "rule_source": "assessment_rules.forbidden_question_types",
                    })
            if ft == "roman_numeral_questions":
                joined = "\n".join(opts)
                if any(token in joined for token in ("I)", "II)", "III)", "IV)")):
                    issues.append({
                        "field": f"section.{sid}.knowledge_check.options",
                        "expected": "no Roman-numeral options",
                        "found": "Roman numerals present",
                        "severity": "warning",
                        "message": (
                            f"The quiz question in '{sec.get('heading')}' uses Roman numerals (I, II, III) "
                            "in its answer options, which is not allowed. Please use regular A/B/C/D options."
                        ),
                        "rule_source": "assessment_rules.forbidden_question_types",
                    })

    return issues


def check_kc_distractor_rationales(sections: list[dict], rule_pack: dict) -> list[dict]:
    """When require_distractor_rationales is True, explanations should reference each option."""
    issues: list[dict] = []
    assessment = rule_pack.get("assessment_rules", {}) or {}
    if not assessment.get("require_distractor_rationales"):
        return issues

    for sec, block in _iter_kc_blocks(sections):
        sid = sec.get("section_id") or sec.get("heading") or "?"
        opts: list[str] = block.get("options") or []
        explanation = (block.get("explanation") or "").strip()
        if not explanation:
            continue  # already flagged as blocker by check_kc_structure

        labels = [chr(ord("A") + i) for i in range(len(opts))]
        missing = [lab for lab in labels if (
            f"{lab})" not in explanation
            and f"{lab}." not in explanation
            and f"option {lab}" not in explanation.lower()
            and f"choice {lab}" not in explanation.lower()
        )]
        if missing:
            issues.append({
                "field": f"section.{sid}.knowledge_check.explanation",
                "expected": "addresses each option (correct + incorrect)",
                "found": f"no reference to {missing}",
                "severity": "warning",
                "message": (
                    f"The explanation for the quiz question in '{sec.get('heading')}' doesn't appear to address "
                    f"all answer options (missing: {missing}). "
                    "The rules require each option — correct and incorrect — to be explained."
                ),
                "rule_source": "assessment_rules.require_distractor_rationales",
            })
    return issues


def check_kc_placement(sections: list[dict], rule_pack: dict) -> list[dict]:
    """Enforce min/max KCs per lesson and forbidden placements."""
    issues: list[dict] = []
    kc_rules = rule_pack.get("kc_placement_rules", {}) or {}
    min_per_lesson = kc_rules.get("min_kc_per_lesson")
    max_per_lesson = kc_rules.get("max_kc_per_lesson")
    forbidden_placements: list[str] = [
        p.lower() for p in (kc_rules.get("forbidden_placements") or [])
    ]

    lesson_kc_counts: list[tuple[str, int]] = []
    current_lesson_heading: str | None = None
    current_kcs = 0

    for sec in sections:
        if sec.get("level") == 1 and not sec.get("is_knowledge_check"):
            if current_lesson_heading is not None:
                lesson_kc_counts.append((current_lesson_heading, current_kcs))
            current_lesson_heading = sec.get("heading", "?")
            current_kcs = 0

        for block in sec.get("body_paragraphs", []) or []:
            if block.get("type") == "knowledge_check":
                current_kcs += 1
                heading_lower = (sec.get("heading") or "").lower()
                for fp in forbidden_placements:
                    needle = fp.replace("_", " ")
                    if needle and needle in heading_lower:
                        issues.append({
                            "field": f"section.{sec.get('section_id') or sec.get('heading')}.knowledge_check",
                            "expected": f"no KC in {fp}",
                            "found": "KC present",
                            "severity": "warning",
                            "message": (
                                f"A quiz question was placed inside '{sec.get('heading')}', "
                                f"which is a section where quiz questions are not allowed (forbidden placement: '{fp}')."
                            ),
                            "rule_source": "kc_placement_rules.forbidden_placements",
                        })

    if current_lesson_heading is not None:
        lesson_kc_counts.append((current_lesson_heading, current_kcs))

    if min_per_lesson is not None:
        for heading, count in lesson_kc_counts:
            if count < int(min_per_lesson):
                issues.append({
                    "field": f"lesson.{heading}.kc_count",
                    "expected": f">= {min_per_lesson}",
                    "found": count,
                    "severity": "warning",
                    "message": (
                        f"Lesson '{heading}' has {count} quiz question(s), but the rules require "
                        f"at least {min_per_lesson} per lesson."
                    ),
                    "rule_source": "kc_placement_rules.min_kc_per_lesson",
                })

    if max_per_lesson is not None:
        for heading, count in lesson_kc_counts:
            if count > int(max_per_lesson):
                issues.append({
                    "field": f"lesson.{heading}.kc_count",
                    "expected": f"<= {max_per_lesson}",
                    "found": count,
                    "severity": "warning",
                    "message": (
                        f"Lesson '{heading}' has {count} quiz question(s), but the rules allow "
                        f"a maximum of {max_per_lesson} per lesson."
                    ),
                    "rule_source": "kc_placement_rules.max_kc_per_lesson",
                })

    return issues
