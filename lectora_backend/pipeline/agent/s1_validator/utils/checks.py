"""
S1 validation checks — validates A0 and A1 outputs against the active rule pack
before content generation (A2) begins.

Each check returns a list of issue dicts:
  {field, expected, found, severity, message, rule_source}

Severities:
  - "blocker"  — pipeline MUST stop; A2 cannot proceed
  - "warning"  — flag for review but allow A2 to proceed
  - "info"     — informational, no action required
"""


# Documented average reading pace per NAIC CE credit-hour formula.
_DEFAULT_WPM = 180

# Difficulty multipliers from NAIC CE Standardized Terms-Definitions.
_DIFFICULTY_MULTIPLIERS: dict[str, float] = {
    "basic": 1.00,
    "intermediate": 1.25,
    "advanced": 1.50,
}


def _total_words_from_sections(sections: list) -> int:
    return sum(s.get("word_count", 0) for s in sections)


def _kc_count_from_sections(sections: list) -> int:
    return sum(1 for s in sections if s.get("has_knowledge_check"))


def _round_credit_hours(hours: float) -> float:
    """Round credit hours: fractional part ≥ 0.50 rounds up, ≤ 0.49 rounds down."""
    whole = int(hours)
    frac = hours - whole
    return float(whole + 1) if frac >= 0.50 else float(whole)


def _difficulty_multiplier(shared_state: dict) -> float:
    """Return the difficulty multiplier from course_metadata; defaults to basic (1.00)."""
    level = (
        shared_state.get("request_spec", {})
        .get("course_metadata", {})
        .get("difficulty_level", "basic")
        or "basic"
    )
    return _DIFFICULTY_MULTIPLIERS.get(level.lower(), 1.00)


def _credit_hours_derived(total_words: int, difficulty_multiplier: float = 1.00) -> float:
    """NAIC formula: words ÷ 180 = minutes; minutes ÷ 50 = base hours; × difficulty."""
    base_hours = total_words / _DEFAULT_WPM / 50
    return _round_credit_hours(base_hours * difficulty_multiplier)


def _credit_hours_from_rule_pack(
    total_words: int, rule_pack: dict, difficulty_multiplier: float = 1.00
) -> float | None:
    """
    Preferred credit-hour derivation using rule-pack pacing (words_per_credit_hour),
    falling back to the NAIC WPM formula when not configured.

    Applies the difficulty multiplier (basic 1.00 / intermediate 1.25 / advanced 1.50)
    after the base calculation.
    """
    pacing = (
        rule_pack.get("content_rules", {}).get("words_per_credit_hour")
        if isinstance(rule_pack, dict)
        else None
    )
    if pacing:
        try:
            base_hours = float(total_words) / float(pacing)
            return _round_credit_hours(base_hours * difficulty_multiplier)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return _credit_hours_derived(total_words, difficulty_multiplier) if total_words > 0 else None


# ── Rule-pack sanity checks ─────────────────────────────────────────────────

def check_rule_pack_sanity(rule_pack: dict) -> list[dict]:
    """
    Validate internal consistency of the active rule pack.

    This is a pre-flight check for *new rule keys* that affect A2 behavior but
    cannot be validated against course content until A2 runs (e.g. KC option counts).
    """
    issues: list[dict] = []
    if not isinstance(rule_pack, dict):
        return issues

    kc_rules = rule_pack.get("kc_placement_rules", {}) if isinstance(rule_pack, dict) else {}
    assessment = rule_pack.get("assessment_rules", {}) if isinstance(rule_pack, dict) else {}

    min_opts = kc_rules.get("min_answer_options")
    max_opts = kc_rules.get("max_answer_options")

    # Option-count bounds should be present for packs that constrain KC options.
    if min_opts is None or max_opts is None:
        issues.append(
            {
                "field": "kc_placement_rules.answer_options_bounds",
                "expected": "min_answer_options and max_answer_options present when KC options are constrained",
                "found": {"min_answer_options": min_opts, "max_answer_options": max_opts},
                "severity": "warning",
                "message": (
                    "The rule pack doesn't define how many answer options quiz questions should have. "
                    "This may cause inconsistent quiz formatting in the generated course."
                ),
                "rule_source": "kc_placement_rules.min_answer_options/max_answer_options",
            }
        )
    else:
        try:
            min_i = int(min_opts)
            max_i = int(max_opts)
            if min_i > max_i:
                issues.append(
                    {
                        "field": "kc_placement_rules.answer_options_bounds",
                        "expected": "min_answer_options <= max_answer_options",
                        "found": {"min_answer_options": min_i, "max_answer_options": max_i},
                        "severity": "blocker",
                        "message": (
                            f"The rule pack has a configuration error: the minimum number of quiz answer options "
                            f"({min_i}) is higher than the maximum ({max_i}). This must be fixed before the course can be generated."
                        ),
                        "rule_source": "kc_placement_rules.min_answer_options/max_answer_options",
                    }
                )
        except (TypeError, ValueError):
            issues.append(
                {
                    "field": "kc_placement_rules.answer_options_bounds",
                    "expected": "min_answer_options/max_answer_options parseable as ints",
                    "found": {"min_answer_options": min_opts, "max_answer_options": max_opts},
                    "severity": "warning",
                    "message": (
                        "The quiz answer option limits in the rule pack are not valid numbers. "
                        "This should be corrected to ensure quiz questions are formatted correctly."
                    ),
                    "rule_source": "kc_placement_rules.min_answer_options/max_answer_options",
                }
            )

    # New flag: require_distractor_rationales
    if "require_distractor_rationales" not in assessment:
        issues.append(
            {
                "field": "assessment_rules.require_distractor_rationales",
                "expected": "present (True/False)",
                "found": "missing",
                "severity": "warning",
                "message": (
                    "The rule pack doesn't specify whether quiz answer explanations must address each wrong option. "
                    "This may affect the quality of quiz explanations in the generated course."
                ),
                "rule_source": "assessment_rules.require_distractor_rationales",
            }
        )

    return issues


# ── A0 Checks ──────────────────────────────────────────────────────────────

def check_a0_metadata(shared_state: dict) -> list[dict]:
    """Verify A0 extracted all required metadata fields."""
    issues = []
    extracted = shared_state.get("extracted_inputs", {})

    # Title must exist
    title = extracted.get("title", "")
    if not title or title == "Unknown":
        issues.append({
            "field": "title",
            "expected": "non-empty course title",
            "found": repr(title),
            "severity": "blocker",
            "message": "A course title could not be found in the uploaded document. A title is required to continue.",
            "rule_source": "A0 metadata extraction",
        })

    # Course ID
    course_id = extracted.get("course_id")
    if not course_id:
        issues.append({
            "field": "course_id",
            "expected": "numeric course ID",
            "found": repr(course_id),
            "severity": "warning",
            "message": "No course ID was found in the document. A default ID may be assigned — please verify this is correct.",
            "rule_source": "A0 metadata extraction",
        })

    # Learning objectives must exist
    los = extracted.get("learning_objectives", [])
    if not los:
        issues.append({
            "field": "learning_objectives",
            "expected": ">= 1 learning objective",
            "found": "0",
            "severity": "blocker",
            "message": "No learning objectives were found in the document. Learning objectives are required to build the course outline.",
            "rule_source": "content_rules.must_map_to_learning_objectives",
        })

    # Content sample should have substance
    sample = extracted.get("content_sample", "")
    if len(sample) < 200:
        issues.append({
            "field": "content_sample",
            "expected": ">= 200 chars",
            "found": f"{len(sample)} chars",
            "severity": "warning",
            "message": "Very little text was extracted from the document. The system may not correctly identify the course type — classification results should be reviewed.",
            "rule_source": "A0 classification quality",
        })

    return issues


def check_a0_classification(shared_state: dict) -> list[dict]:
    """Verify LLM classification confidence and rule pack resolution."""
    issues = []
    llm = shared_state.get("llm_classification", {})
    request_spec = shared_state.get("request_spec", {})

    # Classification confidence
    confidence = llm.get("confidence", 0)
    if confidence < 0.7:
        issues.append({
            "field": "llm_confidence",
            "expected": ">= 0.7",
            "found": confidence,
            "severity": "warning",
            "message": (
                f"The system isn't confident about what type of course this is (confidence score: {confidence}). "
                "The wrong compliance rules may have been applied — a manual review is recommended."
            ),
            "rule_source": "A0 classification",
        })

    # Rule pack must be resolved
    rule_class = request_spec.get("rule_classification", {})
    if not rule_class.get("rule_pack_id"):
        issues.append({
            "field": "rule_pack_id",
            "expected": "resolved rule pack ID",
            "found": "None",
            "severity": "blocker",
            "message": "The system could not determine which compliance rules apply to this course. The process cannot continue without this information.",
            "rule_source": "A0 rule resolution",
        })

    return issues

def check_a0_timed_outline_required(shared_state: dict, rule_pack: dict) -> list[dict]:
    """If timed outline is required, ensure A0 produced TO-outline artefact."""
    issues = []
    if not rule_pack.get("content_rules", {}).get("require_timed_outline"):
        return issues

    to_outline = shared_state.get("llm_to_outline_classification")
    if not to_outline:
        issues.append(
            {
                "field": "llm_to_outline_classification",
                "expected": "present (timed outline required)",
                "found": "missing",
                "severity": "blocker",
                "message": "This course type requires a Timed Outline document, but none was found. Please ensure the Timed Outline was uploaded correctly.",
                "rule_source": "content_rules.require_timed_outline",
            }
        )
        return issues
    if to_outline.get("_no_timed_outline_doc"):
        issues.append(
            {
                "field": "llm_to_outline_classification",
                "expected": "timed outline from uploaded TO document",
                "found": "synthetic outline (no TO .docx provided)",
                "severity": "blocker",
                "message": "This course type requires a Timed Outline document, but none was found. Please ensure the Timed Outline was uploaded correctly.",
                "rule_source": "content_rules.require_timed_outline",
            }
        )
    return issues

def check_a0_images(shared_state: dict) -> list[dict]:
    """Verify image extraction results."""
    issues = []
    images = shared_state.get("images", [])

    # Not a blocker if no images — some docs may not have them
    if not images:
        issues.append({
            "field": "images",
            "expected": ">= 0",
            "found": "0",
            "severity": "info",
            "message": "No images were found in the uploaded document. Images are not required — this is just a note.",
            "rule_source": "A0 image extraction",
        })

    return issues


# ── A1 Checks ──────────────────────────────────────────────────────────────

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

    # Word-count floor only when totals exist on course_spec or sections (A1 may omit both).
    has_word_data = course_spec.get("total_word_count") is not None or any(
        s.get("word_count") is not None for s in sections
    )
    total_words = course_spec.get("total_word_count")
    if total_words is None:
        total_words = _total_words_from_sections(sections)
    if has_word_data and total_words < 100:
        issues.append({
            "field": "total_word_count",
            "expected": ">= 100",
            "found": total_words,
            "severity": "blocker",
            "message": f"The document only produced {total_words} words in the outline — far too little to generate a course. The document may not have been read correctly.",
            "rule_source": "A1 structural integrity",
        })

    # Each content section should have a heading
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

    for sec in course_spec.get("sections", []):
        wc = sec.get("word_count")
        if wc is None:
            continue
        if sec.get("is_knowledge_check"):
            continue
        # Flag very large sections that will need many Lectora pages
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
        kc_count = _kc_count_from_sections(sections)

    # Count L1 sections (lessons)
    lessons = [s for s in sections if s.get(
        "level") == 1 and not s.get("is_knowledge_check")]
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

    los = shared_state.get("extracted_inputs", {}).get(
        "learning_objectives", [])
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
    print(rule_pack.get("content_rules"))
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
        total_words = _total_words_from_sections(sections)

    mult = _difficulty_multiplier(shared_state)
    c_expected = _credit_hours_from_rule_pack(total_words, rule_pack, mult)
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
    return []


def check_a1_assessment_rules(course_spec: dict, rule_pack: dict) -> list[dict]:
    """Verify assessment rules from rule_pack are satisfiable with current structure."""
    issues = []
    assessment = rule_pack.get("assessment_rules", {})

    # 4 answer options required — just record as a pre-flight check
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

    # True/False not allowed
    if not assessment.get("allow_true_false", True):
        issues.append({
            "field": "allow_true_false",
            "expected": "False",
            "found": "False (confirmed)",
            "severity": "info",
            "message": "True/False questions are not allowed for this course type. Only multiple-choice questions (A/B/C/D) will be generated.",
            "rule_source": "assessment_rules.allow_true_false",
        })

    # All-of-the-above not allowed
    if not assessment.get("allow_all_of_the_above", True):
        issues.append({
            "field": "allow_all_of_the_above",
            "expected": "False",
            "found": "False (confirmed)",
            "severity": "info",
            "message": "\"All of the above\" is not allowed as an answer option in this course type.",
            "rule_source": "assessment_rules.allow_all_of_the_above",
        })

    # Objective coverage required
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
