"""
Serialize the active rule pack for LLM prompts so every configured section is visible.

Used by A2 (content generator) — the model receives ``full_rule_pack`` in the user
message as structured JSON.
"""

from __future__ import annotations

# Keys mirrored from ``models.rule_pack.RulePack`` root — entire pack surface.
FULL_RULE_PACK_KEYS: tuple[str, ...] = (
    "id",
    "family",
    "version",
    "audience",
    "dually_registered_iar_context",
    "style_constraints",
    "compliance_elements",
    "content_rules",
    "kc_placement_rules",
    "assessment_rules",
    "deduplication_rules",
    "lectora_constraints",
    "error_tolerance",
)


def bundle_rule_pack_for_prompt(pack: dict) -> dict:
    """Return every rule-pack section needed for authoring (JSON-serializable)."""
    return {k: pack[k] for k in FULL_RULE_PACK_KEYS if k in pack}
