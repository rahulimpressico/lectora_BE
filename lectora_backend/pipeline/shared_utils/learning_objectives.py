"""Helpers for resolving learning objectives across pipeline stages."""

from __future__ import annotations

from typing import Any


def normalize_learning_objectives(items: Any) -> list[str]:
    """Return a clean, ordered list of learning objective strings."""
    if not isinstance(items, list):
        return []

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        cleaned.append(value)
        seen.add(key)
    return cleaned


def is_pdf_only_source(a0_data: dict[str, Any]) -> bool:
    """Return True when the A0 source_document field contains only PDF files."""
    source_document = str(a0_data.get("source_document", "") or "")
    source_names = [part.strip().lower() for part in source_document.split(",") if part.strip()]
    return bool(source_names) and all(name.endswith(".pdf") for name in source_names)


def resolve_learning_objectives(a0_data: dict[str, Any]) -> list[str]:
    """Resolve learning objectives with a PDF-only fallback from A0's LLM outline."""
    extracted_inputs = a0_data.get("extracted_inputs", {}) or {}
    los = normalize_learning_objectives(extracted_inputs.get("learning_objectives", []))
    if los:
        return los

    if not is_pdf_only_source(a0_data):
        return []

    llm_outline = a0_data.get("llm_to_outline_classification", {}) or {}
    return normalize_learning_objectives(llm_outline.get("learning_objectives", []))
