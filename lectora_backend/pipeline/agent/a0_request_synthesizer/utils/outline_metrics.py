"""
Outline Metrics Enricher
========================

After A0 writes ``llm_to_outline.json``, each section (and each subtopic
object within it) should have three timing / pacing fields:

  word_count   — total words in the lesson / subtopic
  minutes      — reading time  (word_count ÷ 180)
  credit_hour  — CE credit     (minutes ÷ 50)

Derivation chain (any one present → the other two are calculated):
  word_count → minutes (÷ 180) → credit_hour (÷ 50)
  minutes    → word_count (× 180) ; credit_hour (÷ 50)
  credit_hour→ minutes (× 50)  ; word_count (× 180)

Works on two subtopic formats:
  • list of strings  → strings are left untouched (no timing data to enrich)
  • list of objects  → each subtopic object is enriched independently
"""

from __future__ import annotations

import copy
import logging

logger = logging.getLogger(__name__)

_WORDS_PER_MINUTE  = 180
_MINUTES_PER_CREDIT = 50


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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


def _enrich_item(item: dict, label: str) -> tuple[dict, bool]:
    """
    Fill in missing word_count / minutes / credit_hour on a single dict
    (works for both top-level sections and subtopic objects).

    Returns (updated_item, was_modified).
    """
    item = dict(item)
    wc   = _to_float(item.get("word_count"))
    mins = _to_float(item.get("minutes"))
    ch   = _to_float(item.get("credit_hour"))
    modified = False

    # ── Derive from word_count ────────────────────────────────────────────
    if wc is not None:
        if mins is None:
            mins = wc / _WORDS_PER_MINUTE
            item["minutes"] = _fmt_minutes(mins)
            logger.debug("[outline_metrics] %s: minutes=%s (from word_count)", label, item["minutes"])
            modified = True
        if ch is None:
            ch = mins / _MINUTES_PER_CREDIT
            item["credit_hour"] = _fmt_credit(ch)
            logger.debug("[outline_metrics] %s: credit_hour=%s (from minutes)", label, item["credit_hour"])
            modified = True

    # ── Derive from minutes ───────────────────────────────────────────────
    elif mins is not None:
        if wc is None:
            wc = mins * _WORDS_PER_MINUTE
            item["word_count"] = _fmt_words(wc)
            logger.debug("[outline_metrics] %s: word_count=%s (from minutes)", label, item["word_count"])
            modified = True
        if ch is None:
            ch = mins / _MINUTES_PER_CREDIT
            item["credit_hour"] = _fmt_credit(ch)
            logger.debug("[outline_metrics] %s: credit_hour=%s (from minutes)", label, item["credit_hour"])
            modified = True

    # ── Derive from credit_hour ───────────────────────────────────────────
    elif ch is not None:
        mins = ch * _MINUTES_PER_CREDIT
        item["minutes"] = _fmt_minutes(mins)
        wc = mins * _WORDS_PER_MINUTE
        item["word_count"] = _fmt_words(wc)
        logger.debug(
            "[outline_metrics] %s: minutes=%s, word_count=%s (from credit_hour)",
            label, item["minutes"], item["word_count"],
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

def enrich_section_metrics(sections: list[dict]) -> tuple[list[dict], bool]:
    """
    Ensure every section — and every subtopic *object* within it — has
    ``word_count``, ``minutes``, and ``credit_hour``.

    Subtopics that are plain strings (flat-document format) are left untouched.

    Returns (enriched_sections, was_modified).
    """
    any_modified = False
    enriched: list[dict] = []

    for idx, raw_sec in enumerate(sections):
        title = raw_sec.get("title", f"section[{idx}]")

        # ── Enrich the section itself ─────────────────────────────────────
        sec, sec_modified = _enrich_item(raw_sec, title)
        if sec_modified:
            any_modified = True

        # ── Enrich subtopic objects (breakdown-document format) ───────────
        subtopics = sec.get("subtopics", [])
        if any(isinstance(s, dict) for s in subtopics):
            enriched_subs: list = []
            for sub in subtopics:
                if isinstance(sub, dict):
                    sub_title = f"{title} → {sub.get('title', '?')}"
                    enriched_sub, sub_mod = _enrich_item(sub, sub_title)
                    if sub_mod:
                        any_modified = True
                    enriched_subs.append(enriched_sub)
                else:
                    enriched_subs.append(sub)   # plain string — untouched
            sec["subtopics"] = enriched_subs

        enriched.append(sec)

    return enriched, any_modified


def enrich_outline_metrics(outline_payload: dict) -> tuple[dict, bool]:
    """
    Top-level entry point: enrich the full ``llm_to_outline`` payload dict.

    Parameters
    ----------
    outline_payload:
        The full JSON object from ``llm_to_outline.json``
        (contains ``"llm_to_outline"`` → ``"sections"``).

    Returns
    -------
    (updated_payload, was_modified)
    """
    outline  = outline_payload.get("llm_to_outline", {})
    sections = outline.get("sections", [])

    if not sections:
        return outline_payload, False

    enriched_sections, modified = enrich_section_metrics(sections)

    if modified:
        updated = copy.deepcopy(outline_payload)
        updated["llm_to_outline"]["sections"] = enriched_sections
        return updated, True

    return outline_payload, False
