"""Helpers for detecting and resolving interactive elements in pipeline stages."""

from __future__ import annotations

from typing import Any, Iterable


INTERACTIVE_TAGS: tuple[str, ...] = (
    "table",
    "figure",
    "image",
    "chart",
    "case study",
    "scenario",
    "video",
    "animation",
)

_IMAGE_CAPTION_HINTS: dict[str, tuple[str, ...]] = {
    "table": ("table",),
    "chart": ("chart", "graph"),
    "figure": ("figure", "diagram", "illustration", "map"),
}


def _append_unique(values: list[str], candidate: str) -> None:
    normalized = candidate.strip().lower()
    if normalized and normalized not in values:
        values.append(normalized)


def collect_interactive_elements(
    texts: Iterable[str],
    *,
    initial: Iterable[str] | None = None,
) -> list[str]:
    """Scan text fragments for known interactive-element keywords."""
    values: list[str] = []
    for item in initial or []:
        _append_unique(values, str(item))

    for text in texts:
        lower = str(text or "").lower()
        for tag in INTERACTIVE_TAGS:
            if tag in lower:
                _append_unique(values, tag)
    return values


def infer_visual_elements_from_images(images: list[dict[str, Any]]) -> list[str]:
    """Infer visual interactive-element tags from mapped image metadata."""
    if not images:
        return []

    inferred: list[str] = ["image"]
    for image in images:
        caption = str(image.get("caption", "") or "").lower()
        heading_context = str(image.get("heading_context", "") or "").lower()
        hint_text = f"{caption} {heading_context}".strip()
        for tag, keywords in _IMAGE_CAPTION_HINTS.items():
            if any(keyword in hint_text for keyword in keywords):
                _append_unique(inferred, tag)
    return inferred


def resolve_section_assets(
    raw_interactive_elements: Iterable[str],
    mapped_images: list[dict[str, Any]],
    *,
    has_knowledge_check: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Resolve the final IE list and images for a course_spec section.

    Rules:
    - Always keep mapped images when present.
    - If raw interactive elements exist, preserve them as the source of truth.
    - If raw interactive elements are empty, infer visual elements from images.
    - Knowledge-check flags are appended consistently.
    """
    resolved_ie = collect_interactive_elements([], initial=raw_interactive_elements)
    if has_knowledge_check:
        _append_unique(resolved_ie, "knowledge_check")

    if not resolved_ie:
        resolved_ie = infer_visual_elements_from_images(mapped_images)
        if has_knowledge_check:
            _append_unique(resolved_ie, "knowledge_check")

    return resolved_ie, list(mapped_images or [])
