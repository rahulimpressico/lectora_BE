"""
LLM classifier and value resolution utilities for A0 — classification step.

Contains: classify_with_llm, resolve_value.
TO-processing functions live in step_03_to_processing/to_processor.py.

Azure OpenAI client/model settings live in config/llm.py.
This module only contains business logic.
"""

import json
import logging
import re
from typing import Any

import json_repair

from ...config.llm import chat
from ...shared.helpers.text_utils import _strip_fences
from lectora_backend.pipeline.shared_llm_config.model_registry import get_deployment
from ..constants.prompts import (
    CLASSIFICATION_PROMPT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def classify_with_llm(
    title: str,
    objectives: list[str],
    content_sample: str,
    *,
    all_doc_titles: list[str] | None = None,
    heading_tree: list[dict] | None = None,
    validation_hints: str | None = None,
) -> dict:
    """Classify the course into a rule family and infer metadata via AzureOpenAI.

    Uses multiple content signals for accurate classification:
      - title: primary title (from first/primary source document)
      - all_doc_titles: titles from every uploaded source document
      - objectives: merged learning objectives from all sources
      - content_sample: representative text from all source documents
      - heading_tree: structured heading hierarchy from all sources

    The richer the signals provided, the more accurate the classification.
    """
    parts: list[str] = []

    # ── Multi-doc title signal ───────────────────────────────────────────────
    if all_doc_titles and len(all_doc_titles) > 1:
        parts.append(
            "## Source Document Titles (" + str(len(all_doc_titles)) + " files)\n"
            + "\n".join(f"- {t}" for t in all_doc_titles if t)
        )
    else:
        parts.append(f"## Course / Document Title\n{title}")

    # ── Learning objectives ──────────────────────────────────────────────────
    if objectives:
        parts.append(
            "## Learning Objectives\n"
            + "\n".join(f"- {obj}" for obj in objectives)
        )

    # ── Document heading structure (strong structural signal) ────────────────
    if heading_tree:
        heading_lines: list[str] = []
        for h in heading_tree[:80]:  # first 80 headings are sufficient
            level = int(h.get("level", 1))
            text = str(h.get("text", "")).strip()
            if text:
                indent = "  " * max(0, level - 1)
                heading_lines.append(f"[L{level}] {indent}{text}")
        if heading_lines:
            parts.append("## Document Heading Structure\n" + "\n".join(heading_lines))

    # ── Content sample ───────────────────────────────────────────────────────
    if content_sample:
        parts.append(f"## Content Sample (from all source files)\n{content_sample}")

    # ── Validation hints ─────────────────────────────────────────────────────
    if validation_hints:
        parts.append(
            "## Prior S1 validation feedback (resolve inconsistencies)\n"
            + validation_hints.strip()
        )

    user_msg = "\n\n".join(parts)

    # ── Logging ─────────────────────────────────────────────────────────────
    logger.info("[CLASSIFY] ══════════════ RULE FAMILY CLASSIFICATION ══════════════")
    logger.info("[CLASSIFY]  Primary title      : %s", title)
    if all_doc_titles:
        logger.info("[CLASSIFY]  All doc titles     : %s", all_doc_titles)
    logger.info("[CLASSIFY]  Objectives         : %d items", len(objectives))
    logger.info("[CLASSIFY]  Heading entries    : %d", len(heading_tree) if heading_tree else 0)
    logger.info(
        "[CLASSIFY]  Content sample     : %d chars",
        len(content_sample) if content_sample else 0,
    )
    logger.info("[CLASSIFY]  Sending to LLM (model=A0 → %s)…", get_deployment("A0"))

    raw = chat(CLASSIFICATION_PROMPT, user_msg)

    logger.info("[CLASSIFY]  LLM raw response   : %s", raw[:300].replace("\n", " "))

    try:
        result = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as original_exc:
        logger.warning(
            "[CLASSIFY] Invalid JSON from LLM — attempting json_repair. "
            "Raw response (first 500 chars): %r",
            raw[:500],
        )
        try:
            repaired = json_repair.repair_json(_strip_fences(raw), return_objects=True)
            if isinstance(repaired, list) and repaired and all(isinstance(i, dict) for i in repaired):
                repaired = {"sections": repaired}
            if not isinstance(repaired, dict):
                raise ValueError(
                    f"json_repair returned {type(repaired).__name__}, expected dict"
                )
            logger.info("[CLASSIFY] json_repair successfully recovered malformed classification JSON.")
            result = repaired
        except Exception as repair_exc:
            raise ValueError(
                f"LLM returned invalid JSON for course classification and repair failed. "
                f"Original error: {original_exc}. "
                f"Repair error: {repair_exc}. "
                f"Raw output (first 500 chars): {raw[:500]!r}"
            ) from original_exc

    logger.info(
        "[CLASSIFY]  ── RESULT: rule_family=%s | confidence=%.2f | topic=%s",
        result.get("rule_family"),
        float(result.get("confidence") or 0),
        result.get("topic"),
    )
    logger.info("[CLASSIFY]  ── REASONING: %s", result.get("reasoning", ""))
    logger.info("[CLASSIFY] ══════════════════════════════════════════════════════════")

    return result


def resolve_value(
    key: str, explicit: dict, rule_defaults: dict, inferred: dict
) -> tuple[Any, str]:
    """
    Resolve a value from three sources in priority order.

    Returns (value, source) where source is one of:
      'explicitly_provided', 'derived_from_rule_pack', 'inferred'
    """
    if key in explicit and explicit[key] is not None:
        return explicit[key], "explicitly_provided"
    if key in rule_defaults and rule_defaults[key] is not None:
        return rule_defaults[key], "derived_from_rule_pack"
    if key in inferred and inferred[key] is not None:
        return inferred[key], "inferred"
    return None, "unresolved"
