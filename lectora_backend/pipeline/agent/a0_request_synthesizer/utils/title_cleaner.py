"""
Title cleaner — post-processes LLM-extracted outline titles.

Removes layout artefacts that occasionally leak through from the source TO
document (e.g. trailing "page N" references). Run this BEFORE
``llm_to_outline.json`` is consumed downstream.

Idempotent: running twice yields the same result.
"""

from __future__ import annotations

import re

# Trailing ", page 3", " page 3", " pg 3", " p. 3", " (page 3)" — case-insensitive
_TRAILING_PAGE_RE = re.compile(
    r"""
    [\s,;:\-–—()]*           # optional separators / opening bracket
    \b
    (?:page|pg\.?|p\.?)      # page / pg / pg. / p / p.
    \s*[#:]?\s*              # optional # or :
    \d{1,4}                  # the page number
    \)?                      # optional closing bracket
    \s*$                     # end of string
    """,
    re.IGNORECASE | re.VERBOSE,
)


def clean_title(title: str) -> str:
    """Strip trailing 'page N' style references from a single title.

    Examples:
        "1.0 Anywhere There Is Water page 1" -> "1.0 Anywhere There Is Water"
        "2.3 Ineligible Property pg 3"       -> "2.3 Ineligible Property"
        "5.6 Cancellations (page 22)"        -> "5.6 Cancellations"
        "Knowledge Check page 5"             -> "Knowledge Check"
    """
    if not isinstance(title, str):
        return title
    cleaned = _TRAILING_PAGE_RE.sub("", title).rstrip(" ,;:-–—")
    return cleaned.strip()


def clean_outline_titles(outline_payload: dict) -> tuple[dict, int]:
    """Walk the outline payload and clean every section / subtopic title in place.

    Returns the (mutated) payload and the number of titles modified.
    """
    if not isinstance(outline_payload, dict):
        return outline_payload, 0

    sections = outline_payload.get("sections")
    if not isinstance(sections, list):
        return outline_payload, 0

    n_changed = 0

    for sec in sections:
        if not isinstance(sec, dict):
            continue

        original = sec.get("title", "")
        cleaned = clean_title(original)
        if cleaned != original:
            sec["title"] = cleaned
            n_changed += 1

        subs = sec.get("subtopics") or []
        for i, sub in enumerate(subs):
            if isinstance(sub, str):
                cleaned_s = clean_title(sub)
                if cleaned_s != sub:
                    subs[i] = cleaned_s
                    n_changed += 1
            elif isinstance(sub, dict):
                orig_t = sub.get("title", "")
                cleaned_t = clean_title(orig_t)
                if cleaned_t != orig_t:
                    sub["title"] = cleaned_t
                    n_changed += 1

    return outline_payload, n_changed
