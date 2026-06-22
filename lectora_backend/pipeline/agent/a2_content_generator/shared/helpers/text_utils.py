"""Shared text utilities for A2 pipeline steps."""
import re


def _strip_fences(raw: str) -> str:
    """Remove markdown code fences that some models insert."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text.rstrip())
    return text.strip()
