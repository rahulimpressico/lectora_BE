"""
Merge a base rule pack with a difficulty-level overlay (basic / intermediate / advanced).
"""

from __future__ import annotations

import copy
from typing import Any

VALID_DIFFICULTIES: tuple[str, ...] = ("basic", "intermediate", "advanced")

# Keys copied from base only — overlays do not replace these wholesale
_PRESERVE_BASE_KEYS = frozenset({"difficulty_levels", "default_difficulty", "id", "family", "version"})


def _merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **val}
        else:
            out[key] = val
    return out


def apply_difficulty_overlay(
    base_pack: dict[str, Any],
    overlay: dict[str, Any],
    difficulty_key: str,
) -> dict[str, Any]:
    """
    Return a new rule pack dict: base settings with section-level fields overridden
    by the difficulty overlay. Sets ``active_difficulty`` on the result.
    """
    merged = copy.deepcopy(base_pack)
    for key, val in overlay.items():
        if key in _PRESERVE_BASE_KEYS:
            continue
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], val)
        else:
            merged[key] = copy.deepcopy(val)
    merged["active_difficulty"] = difficulty_key
    return merged


def normalize_difficulty(
    difficulty: str | None,
    *,
    default: str = "intermediate",
    levels: dict | None = None,
) -> str:
    key = (difficulty or default).strip().lower()
    if levels and key not in levels:
        valid = ", ".join(sorted(levels.keys()))
        raise ValueError(
            f"Unknown course difficulty '{difficulty}'. Valid levels: {valid}"
        )
    if key not in VALID_DIFFICULTIES:
        raise ValueError(
            f"Unknown course difficulty '{difficulty}'. "
            f"Valid levels: {', '.join(VALID_DIFFICULTIES)}"
        )
    return key
