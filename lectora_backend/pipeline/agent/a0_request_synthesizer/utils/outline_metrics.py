"""
Outline Metrics Enricher
========================

After A0 writes ``llm_to_outline.json``, each section (and each subtopic
object within it) should have three timing / pacing fields:

  word_count   — total words in the lesson / subtopic
  minutes      — reading time  (word_count ÷ 180)
  credit_hour  — CE credit with difficulty factor applied
                 (minutes ÷ 50) × difficulty_factor
                 Rounded using NAIC rule: fractional ≥ 0.50 → round up,
                 fractional < 0.50 → round down.

NAIC CE standard constants:
  180 words  = 1 minute reading time
  50 minutes = 1 base CE credit hour
  9,000 words = 1 base CE credit hour

Difficulty multipliers (NAIC CE Standardized Terms):
  basic        1.00×
  intermediate 1.25×
  advanced     1.50×

Derivation chain (any one present → the other two are calculated):
  word_count → minutes (÷ 180) → credit_hour (minutes ÷ 50) × factor
  minutes    → word_count (× 180) ; credit_hour (minutes ÷ 50) × factor
  credit_hour→ minutes (× 50 ÷ factor) ; word_count (minutes × 180)

Works on two subtopic formats:
  • list of strings  → strings are left untouched (no timing data to enrich)
  • list of objects  → each subtopic object is enriched independently
"""

from __future__ import annotations

import copy
import logging
import math

logger = logging.getLogger(__name__)

_WORDS_PER_MINUTE   = 180
_MINUTES_PER_CREDIT = 50
_WORDS_PER_CE_HOUR  = _WORDS_PER_MINUTE * _MINUTES_PER_CREDIT  # 9,000

# NAIC CE Standardized difficulty multipliers
DIFFICULTY_FACTORS: dict[str, float] = {
    "basic":        1.00,
    "intermediate": 1.25,
    "advanced":     1.50,
}
_DEFAULT_DIFFICULTY = "intermediate"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def get_difficulty_factor(difficulty: str) -> float:
    """Return the NAIC CE difficulty multiplier for the given difficulty string."""
    return DIFFICULTY_FACTORS.get((difficulty or "").strip().lower(), 1.25)


def naic_round(hours: float) -> float:
    """NAIC rounding rule: fractional part ≥ 0.50 rounds up; < 0.50 rounds down."""
    whole = math.floor(hours)
    frac  = hours - whole
    return float(whole + 1) if frac >= 0.50 else float(whole)


def words_to_credit_hours(word_count: int | float, difficulty: str = _DEFAULT_DIFFICULTY) -> float:
    """Convert a word count to CE credit hours (with difficulty factor + NAIC rounding)."""
    factor = get_difficulty_factor(difficulty)
    raw_hours = (word_count / _WORDS_PER_CE_HOUR) * factor
    return naic_round(raw_hours)


def _to_float(val) -> float | None:
    """Convert a raw field value to a positive float, or return None."""
    if val is None:
        return None
    if isinstance(val, str) and not val.strip():
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _fmt_minutes(val: float) -> str:
    return str(round(val, 2))


def _fmt_credit(val: float) -> str:
    return str(round(val, 4))


def _fmt_words(val: float) -> str:
    return str(int(round(val)))


def _enrich_item(
    item: dict,
    label: str,
    difficulty_factor: float = 1.25,
) -> tuple[dict, bool]:
    """Fill in missing word_count / minutes / credit_hour on a single dict.

    credit_hour is always computed as (minutes / 50) × difficulty_factor so
    that harder courses correctly accumulate more CE credit for the same
    reading volume.

    Returns (updated_item, was_modified).
    """
    item     = dict(item)
    wc       = _to_float(item.get("word_count"))
    mins     = _to_float(item.get("minutes"))
    ch       = _to_float(item.get("credit_hour"))
    modified = False

    # ── Derive from word_count ────────────────────────────────────────────
    if wc is not None:
        if mins is None:
            mins = wc / _WORDS_PER_MINUTE
            item["minutes"] = _fmt_minutes(mins)
            logger.debug("[outline_metrics] %s: minutes=%s (from word_count)", label, item["minutes"])
            modified = True
        if ch is None:
            ch = (mins / _MINUTES_PER_CREDIT) * difficulty_factor
            item["credit_hour"] = _fmt_credit(ch)
            logger.debug("[outline_metrics] %s: credit_hour=%s (factor=%.2f)", label, item["credit_hour"], difficulty_factor)
            modified = True

    # ── Derive from minutes ───────────────────────────────────────────────
    elif mins is not None:
        if wc is None:
            wc = mins * _WORDS_PER_MINUTE
            item["word_count"] = _fmt_words(wc)
            logger.debug("[outline_metrics] %s: word_count=%s (from minutes)", label, item["word_count"])
            modified = True
        if ch is None:
            ch = (mins / _MINUTES_PER_CREDIT) * difficulty_factor
            item["credit_hour"] = _fmt_credit(ch)
            logger.debug("[outline_metrics] %s: credit_hour=%s (factor=%.2f)", label, item["credit_hour"], difficulty_factor)
            modified = True

    # ── Derive from credit_hour ───────────────────────────────────────────
    elif ch is not None:
        # Reverse: credit_hour already encodes the difficulty factor, so
        # remove it before recovering reading-time minutes and word count.
        base_mins = (ch / difficulty_factor) * _MINUTES_PER_CREDIT if difficulty_factor > 0 else ch * _MINUTES_PER_CREDIT
        item["minutes"]    = _fmt_minutes(base_mins)
        item["word_count"] = _fmt_words(base_mins * _WORDS_PER_MINUTE)
        logger.debug(
            "[outline_metrics] %s: minutes=%s, word_count=%s (from credit_hour, factor=%.2f)",
            label, item["minutes"], item["word_count"], difficulty_factor,
        )
        modified = True

    else:
        logger.warning(
            "[outline_metrics] %s: no source value for word_count / minutes / credit_hour — "
            "fields left empty.",
            label,
        )

    return item, modified


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_section_metrics(
    sections: list[dict],
    difficulty: str = _DEFAULT_DIFFICULTY,
) -> tuple[list[dict], bool]:
    """Ensure every section — and every subtopic object within it — has
    ``word_count``, ``minutes``, and ``credit_hour``.

    ``difficulty`` controls the CE multiplier applied to credit_hour:
      basic 1.00×, intermediate 1.25×, advanced 1.50×.

    Subtopics that are plain strings (flat-document format) are left untouched.

    Returns (enriched_sections, was_modified).
    """
    factor       = get_difficulty_factor(difficulty)
    any_modified = False
    enriched: list[dict] = []

    for idx, raw_sec in enumerate(sections):
        title = raw_sec.get("title", f"section[{idx}]")

        # ── Enrich the section itself ─────────────────────────────────────
        sec, sec_modified = _enrich_item(raw_sec, title, difficulty_factor=factor)
        if sec_modified:
            any_modified = True

        # ── Enrich subtopic objects (breakdown-document format) ───────────
        subtopics = sec.get("subtopics", [])
        if any(isinstance(s, dict) for s in subtopics):
            enriched_subs: list = []
            for sub in subtopics:
                if isinstance(sub, dict):
                    sub_title  = f"{title} → {sub.get('title', '?')}"
                    enriched_sub, sub_mod = _enrich_item(sub, sub_title, difficulty_factor=factor)
                    if sub_mod:
                        any_modified = True
                    enriched_subs.append(enriched_sub)
                else:
                    enriched_subs.append(sub)   # plain string — untouched
            sec["subtopics"] = enriched_subs

        enriched.append(sec)

    return enriched, any_modified


def enrich_outline_metrics(
    outline_payload: dict,
    difficulty: str = _DEFAULT_DIFFICULTY,
) -> tuple[dict, bool]:
    """Top-level entry point: enrich the full ``llm_to_outline`` payload dict.

    Parameters
    ----------
    outline_payload:
        Full JSON object from ``llm_to_outline.json``
        (contains ``"llm_to_outline"`` → ``"sections"``).
    difficulty:
        Course difficulty string — "basic", "intermediate", or "advanced".
        Passed through to ``enrich_section_metrics`` to apply the correct
        NAIC CE credit-hour multiplier.

    Returns
    -------
    (updated_payload, was_modified)
    """
    outline  = outline_payload.get("llm_to_outline", {})
    sections = outline.get("sections", [])

    if not sections:
        return outline_payload, False

    enriched_sections, modified = enrich_section_metrics(sections, difficulty=difficulty)

    if modified:
        updated = copy.deepcopy(outline_payload)
        updated["llm_to_outline"]["sections"] = enriched_sections
        return updated, True

    return outline_payload, False


def compute_course_totals(
    sections: list[dict],
    difficulty: str = _DEFAULT_DIFFICULTY,
) -> dict:
    """Compute aggregate totals for a list of enriched sections.

    Returns a dict with:
      total_word_count    — sum of section word_count values
      total_minutes       — total reading time
      total_credit_hours  — CE hours with difficulty factor + NAIC rounding
      difficulty_factor   — the multiplier applied
    """
    factor      = get_difficulty_factor(difficulty)
    total_words = sum(
        int(float(s.get("word_count") or 0)) for s in sections
    )
    total_mins  = total_words / _WORDS_PER_MINUTE
    raw_hours   = (total_mins / _MINUTES_PER_CREDIT) * factor
    total_hours = naic_round(raw_hours)

    return {
        "total_word_count":   total_words,
        "total_minutes":      round(total_mins, 2),
        "total_credit_hours": total_hours,
        "difficulty_factor":  factor,
    }
