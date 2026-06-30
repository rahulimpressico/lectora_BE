from __future__ import annotations

# Documented average reading pace per NAIC CE credit-hour formula.
_DEFAULT_WPM = 180

# Difficulty multipliers from NAIC CE Standardized Terms-Definitions.
_DIFFICULTY_MULTIPLIERS: dict[str, float] = {
    "basic": 1.00,
    "intermediate": 1.25,
    "advanced": 1.50,
}


def total_words_from_sections(sections: list) -> int:
    return sum(s.get("word_count", 0) for s in sections)


def kc_count_from_sections(sections: list) -> int:
    return sum(1 for s in sections if s.get("has_knowledge_check"))


def round_credit_hours(hours: float) -> float:
    """Round credit hours: fractional part ≥ 0.50 rounds up, ≤ 0.49 rounds down."""
    whole = int(hours)
    frac = hours - whole
    return float(whole + 1) if frac >= 0.50 else float(whole)


def difficulty_multiplier(shared_state: dict) -> float:
    """Return the difficulty multiplier from course_metadata; defaults to basic (1.00)."""
    level = (
        shared_state.get("request_spec", {})
        .get("course_metadata", {})
        .get("difficulty_level", "basic")
        or "basic"
    )
    return _DIFFICULTY_MULTIPLIERS.get(level.lower(), 1.00)


def credit_hours_derived(total_words: int, difficulty_multiplier_value: float = 1.00) -> float:
    """NAIC formula: words ÷ 180 = minutes; minutes ÷ 50 = base hours; × difficulty."""
    base_hours = total_words / _DEFAULT_WPM / 50
    return round_credit_hours(base_hours * difficulty_multiplier_value)


def credit_hours_from_rule_pack(
    total_words: int,
    rule_pack: dict,
    difficulty_multiplier_value: float = 1.00,
) -> float | None:
    """
    Preferred credit-hour derivation using rule-pack pacing (words_per_credit_hour),
    falling back to the NAIC WPM formula when not configured.
    """
    pacing = (
        rule_pack.get("content_rules", {}).get("words_per_credit_hour")
        if isinstance(rule_pack, dict)
        else None
    )
    if pacing:
        try:
            base_hours = float(total_words) / float(pacing)
            return round_credit_hours(base_hours * difficulty_multiplier_value)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return (
        credit_hours_derived(total_words, difficulty_multiplier_value)
        if total_words > 0
        else None
    )
