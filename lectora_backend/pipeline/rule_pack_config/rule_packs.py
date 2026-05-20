"""
Rule Packs — single source of truth for assessment rules, style constraints,
compliance elements, KC placement, Lectora limits, and compliance notes.

Shared across all agents (A0–A2, S1).

Each family’s dict lives in ``packs/<key>.py`` as ``PACK``; this module only maps keys.
"""


from __future__ import annotations

from .difficulty_merge import apply_difficulty_overlay, normalize_difficulty
from .packs import firm_element as _firm_element
from .packs import iarce as _iarce
from .packs import insurance_ce as _insurance_ce

RULE_PACKS = {
    "insurance_ce": _insurance_ce.PACK,
    "iarce": _iarce.PACK,
    "firm_element": _firm_element.PACK,
}

# ── Helpers ─────────────────────────────────────────────────────────────────

def get_rule_pack(rule_family: str) -> dict | None:
    """Look up a full rule pack by family name (e.g. 'Insurance CE').

    Tries exact key match first, then matches on the ``family`` field.
    Returns None if nothing matches.
    """
    if rule_family in RULE_PACKS:
        return RULE_PACKS[rule_family]

    for _key, pack in RULE_PACKS.items():
        if pack["family"] == rule_family:
            return pack

    return None


def resolve_rule_pack(
    rule_family: str,
    difficulty: str | None = None,
) -> dict | None:
    """
    Resolve a full rule pack for a family.

    When the pack defines ``difficulty_levels`` (basic / intermediate / advanced),
    merges the overlay for ``difficulty`` (default: pack's ``default_difficulty``
    or ``intermediate``). Result includes ``active_difficulty``.
    """
    pack = get_rule_pack(rule_family)
    if not pack:
        return None

    levels = pack.get("difficulty_levels")
    if not isinstance(levels, dict) or not levels:
        return pack

    default = pack.get("default_difficulty") or "intermediate"
    key = normalize_difficulty(difficulty, default=default, levels=levels)
    overlay = levels[key]
    return apply_difficulty_overlay(pack, overlay, key)
