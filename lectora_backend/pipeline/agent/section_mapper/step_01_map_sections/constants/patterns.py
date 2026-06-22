"""Regex patterns and heading-classification helpers for section mapping."""
import re

# Structural sections that must never act as content-topic containers.
_RESERVED_HEADING_RE = re.compile(
    r"^\s*(\d+(\.\d+)*\s+)?"
    r"(overview|learning\s+objectives?|learning\s+outcomes?|course\s+objectives?|"
    r"summary|assessment|introduction)\s*$",
    re.IGNORECASE,
)

# Number prefix at start of a title:  "2.1", "3.2.1", "10.4"
_NUM_PREFIX_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,3})\b")


def _is_reserved_heading(heading: str) -> bool:
    return bool(_RESERVED_HEADING_RE.match((heading or "").strip()))
