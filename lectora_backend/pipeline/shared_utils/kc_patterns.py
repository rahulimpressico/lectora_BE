"""
Shared regex patterns and helpers for Knowledge Check detection.

Used by section_mapper and kc_planner — kept here to avoid duplication.
"""

import re

# Matches any heading that references a "Knowledge Check" section.
KC_RE = re.compile(r"knowledge\s*check", re.IGNORECASE)


def is_kc_title(title: str) -> bool:
    """Return True if *title* looks like a Knowledge Check heading."""
    return bool(KC_RE.search(title or ""))
