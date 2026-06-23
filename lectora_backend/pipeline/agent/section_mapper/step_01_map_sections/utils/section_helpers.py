"""
Section-mapping structural helpers.

Only two utilities remain after the vector-retrieval refactor:
  - _is_breakdown_format: detect which TO outline format is in use.
  - _clean_ie: sanitise interactive_elements lists.

All former fuzzy-matching distribution logic has been removed; content
retrieval is now handled exclusively by vector_retriever.py.
"""
from __future__ import annotations


def _is_breakdown_format(to_sections: list[dict]) -> bool:
    """Return True when at least one TO section carries subtopics as objects (Format 1)."""
    return any(
        isinstance(sub, dict)
        for sec in to_sections
        for sub in sec.get("subtopics", [])
    )


def _clean_ie(ie_list: list) -> list[str]:
    """Return a sanitised list of interactive-element strings, excluding blank / 'n/a' entries."""
    return [str(e) for e in ie_list if e and str(e).strip().lower() != "n/a"]
