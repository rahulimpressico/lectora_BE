"""Shared text utilities for A0 pipeline steps."""

import re


def _strip_fences(text: str) -> str:
    """Remove markdown code fences that some models insert."""
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()
