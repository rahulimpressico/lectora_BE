"""Shared section-classification helpers for A1."""
import re
from typing import Any

_RESERVED_SECTION_RE = re.compile(
    r"^\s*(\d+(\.\d+)*\s+)?"
    r"(overview|learning\s+objectives?|learning\s+outcomes?|course\s+objectives?|"
    r"summary|assessment|introduction)\s*$",
    re.IGNORECASE,
)


def _is_reserved_section(heading: str) -> bool:
    """Return True if heading names a structural section that must not hold subtopics."""
    return bool(_RESERVED_SECTION_RE.match(heading.strip()))


def _normalize_section_level(level: Any) -> int:
    """Clamp section levels into the schema-supported range (1..4)."""
    try:
        value = int(level)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(value, 4))
