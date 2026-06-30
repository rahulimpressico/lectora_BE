from __future__ import annotations

from .common import (
    credit_hours_from_rule_pack,
    difficulty_multiplier,
    kc_count_from_sections,
    total_words_from_sections,
)


def check_a1_sections(course_spec: dict, rule_pack: dict) -> list[dict]:
    """Verify A1 parsed sections meet structural requirements."""
    issues = []
    sections = course_spec.get("sections", [])

    if not sections:
        issues.append({
            "field": "sections",
            "expected": ">= 1 section",
            "found": "0",
            "severity": "blocker",
            "message": "The outline builder produced no sections. The document may not have been read correctly — please check the uploaded file.",
            "rule_source": "A1 parse_document",
        })
        return issues

    has_word_data = course_spec.get("total_word_count") is not None or any(
        s.get("word_count") is not None for s in sections
    )
    total_words = course_spec.get("total_word_count")
    if total_words is None:
        total_words = total_words_from_sections(sections)
    if has_word_data and total_words < 100:
        issues.append({
            "field": "total_word_count",
            "expected": ">= 100",
            "found": total_words,
            "severity": "blocker",
            "message": f"The document only produced {total_words} words in the outline — far too little to generate a course. The document may not have been read correctly.",
            "rule_source": "A1 structural integrity",
        })

    for sec in sections:
        if not sec.get("heading"):
            issues.append({
                "field": f"section.{sec.get('id', '?')}.heading",
                "expected": "non-empty heading",
                "found": "empty",
                "severity": "warning",
                "message": f"Section {sec.get('id', '?')} has no title. Every section needs a heading.",
                "rule_source": "content_rules.maintain_section_boundary_integrity",
            })

    return issues


def check_a1_word_counts(course_spec: dict, rule_pack: dict) -> list[dict]:
    """Check word count tolerance per section against Lectora constraints."""
    issues = []
    lectora = rule_pack.get("lectora_constraints", {})
    max_per_page = lectora.get("max_words_per_page")

    if not max_per_page:
        return issues

    for sec in course_spec.get("sections", []):
        wc = sec.get("word_count")
        if wc is None:
            continue
        if sec.get("is_knowledge_check"):
            continue
        if wc > max_per_page * 10:
            issues.append({
                "field": f"section.{sec.get('id', '?')}.word_count",
                "expected": f"<= {max_per_page * 10} (Lectora ~10 pages max)",
                "found": wc,
                "severity": "info",
                "message": (
                    f"Section '{sec.get('heading', '?')}' has {wc} words "
                    f"(about {wc // max_per_page} Lectora pages), which may be too long to display well. "
                    "Consider splitting this section into smaller parts."
                ),
                "rule_source": "lectora_constraints.max_words_per_page",
            })

    return issues


def check_a1_kc_count(course_spec: dict, rule_pack: dict) -> list[dict]:
    """Verify KC count against rule pack minimums."""
    issues = []
    kc_rules = rule_pack.get("kc_placement_rules", {})
    min_per_lesson = kc_rules.get("min_kc_per_lesson", 2)

    sections = course_spec.get("sections", [])
    kc_count = course_spec.get("knowledge_check_count")
    if kc_count is None:
        kc_count = kc_count_from_sections(sections)

    lessons = [s for s in sections if s.get("level") == 1 and not s.get("is_knowledge_check")]
    lesson_count = max(len(lessons), 1)

    expected_min = min_per_lesson * lesson_count
    if kc_count < expected_min:
        issues.append({
            "field": "knowledge_check_count",
            "expected": f">= {expected_min} ({min_per_lesson}/lesson x {lesson_count} lessons)",
            "found": kc_count,
            "severity": "warning",
            "message": (
                f"Only {kc_count} quiz question(s) were found in the outline, but the rules require "
                f"at least {min_per_lesson} per lesson ({expected_min} total across {lesson_count} lesson(s)). "
                "The content generator will add more questions to meet this requirement."
            ),
            "rule_source": "kc_placement_rules.min_kc_per_lesson",
        })

    return issues


def check_a1_lo_coverage(course_spec: dict, shared_state: dict, rule_pack: dict) -> list[dict]:
    """Verify all learning objectives are mapped to at least one section."""
    issues = []
    if not rule_pack.get("content_rules", {}).get("must_map_to_learning_objectives", True):
        return issues

    los = shared_state.get("extracted_inputs", {}).get("learning_objectives", [])
    if not los:
        return issues

    mapped = set()
    for sec in course_spec.get("sections", []):
        mapped.update(sec.get("maps_to_objectives", []))

    unmapped = [i for i in range(len(los)) if i not in mapped]
    if unmapped:
        lo_labels = [f"LO-{i} ({los[i][:50]}...)" for i in unmapped]
        issues.append({
            "field": "learning_objectives_coverage",
            "expected": f"all {len(los)} LOs mapped",
            "found": f"{len(unmapped)} unmapped",
            "severity": "warning",
            "message": (
                f"The following learning objectives are not linked to any course section: {', '.join(lo_labels)}. "
                "The content writing stage should ensure these topics are covered."
            ),
            "rule_source": "content_rules.must_map_to_learning_objectives",
        })

    return issues


def check_a1_learning_objectives_range(shared_state: dict, rule_pack: dict) -> list[dict]:
    """Enforce LO count range when configured (e.g. [5,10] for IARCE/FE)."""
    issues = []
    rng = rule_pack.get("content_rules", {}).get("learning_objectives_range")
    if not rng or not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return issues

    los = shared_state.get("extracted_inputs", {}).get("learning_objectives", []) or []
    try:
        lo_count = len(los)
        lo_min, lo_max = int(rng[0]), int(rng[1])
    except (TypeError, ValueError, IndexError):
        return issues

    if lo_count < lo_min or lo_count > lo_max:
        issues.append(
            {
                "field": "learning_objectives",
                "expected": f"{lo_min}–{lo_max} learning objectives",
                "found": lo_count,
                "severity": "blocker",
                "message": (
                    f"This course type requires between {lo_min} and {lo_max} learning objectives, "
                    f"but {lo_count} were found. Please update the learning objectives before continuing."
                ),
                "rule_source": "content_rules.learning_objectives_range",
            }
        )
    return issues


def check_a1_credit_hours_against_rule_pack(course_spec: dict, shared_state: dict, rule_pack: dict) -> list[dict]:
    """Cross-check credit-hours using rule-pack words_per_credit_hour when available."""
    issues = []
    sections = course_spec.get("sections", [])
    total_words = course_spec.get("total_word_count")
    if total_words is None:
        total_words = total_words_from_sections(sections)

    mult = difficulty_multiplier(shared_state)
    c_expected = credit_hours_from_rule_pack(total_words, rule_pack, mult)
    if c_expected is None:
        return issues

    diff_level = (
        shared_state.get("request_spec", {})
        .get("course_metadata", {})
        .get("difficulty_level", "basic")
        or "basic"
    )
    issues.append({
        "field": "credit_hours",
        "expected": str(c_expected),
        "found": str(c_expected),
        "severity": "info",
        "message": (
            f"Estimated credit hours: {c_expected} "
            f"(difficulty: {diff_level}, multiplier: ×{mult})"
        ),
        "rule_source": "content_rules.words_per_credit_hour",
    })
    return issues


def check_a1_credit_hours(course_spec: dict, shared_state: dict) -> list[dict]:
    """Legacy hook: credit_hours is no longer stored on request_spec, so nothing to compare."""
    _ = course_spec
    _ = shared_state
    return []


def check_a1_assessment_rules(course_spec: dict, rule_pack: dict) -> list[dict]:
    """Verify assessment rules from rule_pack are satisfiable with current structure."""
    issues = []
    assessment = rule_pack.get("assessment_rules", {})

    opts = assessment.get("answer_options_count", 4)
    if opts != 4:
        issues.append({
            "field": "answer_options_count",
            "expected": 4,
            "found": opts,
            "severity": "info",
            "message": f"This course is configured to use {opts} answer options per quiz question (not the usual 4). This is just a note.",
            "rule_source": "assessment_rules.answer_options_count",
        })

    if not assessment.get("allow_true_false", True):
        issues.append({
            "field": "allow_true_false",
            "expected": "False",
            "found": "False (confirmed)",
            "severity": "info",
            "message": "True/False questions are not allowed for this course type. Only multiple-choice questions (A/B/C/D) will be generated.",
            "rule_source": "assessment_rules.allow_true_false",
        })

    if not assessment.get("allow_all_of_the_above", True):
        issues.append({
            "field": "allow_all_of_the_above",
            "expected": "False",
            "found": "False (confirmed)",
            "severity": "info",
            "message": "\"All of the above\" is not allowed as an answer option in this course type.",
            "rule_source": "assessment_rules.allow_all_of_the_above",
        })

    if assessment.get("objective_coverage_required", False):
        sections = course_spec.get("sections", [])
        mapped = set()
        for s in sections:
            mapped.update(s.get("maps_to_objectives", []))
        if not mapped:
            issues.append({
                "field": "objective_coverage",
                "expected": "at least some LOs mapped",
                "found": "0 mappings",
                "severity": "warning",
                "message": "The rules require quiz questions to cover the learning objectives, but no objectives have been assigned to any section yet.",
                "rule_source": "assessment_rules.objective_coverage_required",
            })

    return issues
