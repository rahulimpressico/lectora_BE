"""
A1 — Timed Outline Interpreter (LangGraph)

Architecture
------------
  Parser owns ALL structural data (sections, word counts, KCs, interactive elements).
  Azure OpenAI is called ONLY to enrich sections with subtopics + LO mapping.
  This prevents the LLM from accidentally returning empty sections.

Graph
-----
  load_shared_state
      |
  parse_document          <- retry once on failure, then FAILED
      |
  validate_los            <- STOP (critical) if learning objectives missing
      |
  map_images              <- pure code, no LLM
      |
  enrich_with_llm         <- Azure OpenAI: subtopics + LO mapping only
      |
  build_course_spec       <- code assembles final spec from parsed + enriched data
      |
  detect_inconsistencies
      |
  persist_output
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from docx import Document
from langgraph.graph import StateGraph, END
from typing_extensions import TypedDict

from .config.llm import chat
from .prompt.enrichment import ENRICH_SYSTEM
from .utils.helpers import count_words, words_to_minutes, wpm_from_rule_pack, to_snake
from .utils.image_mapper import map_images_to_sections

from lectora_backend.pipeline.models import A1Output, A1Status, CourseSpec, Inconsistency
from lectora_backend.pipeline.rule_pack_config.rule_packs import resolve_rule_pack
from lectora_backend.pipeline.shared_utils.interactive_elements import (
    collect_interactive_elements,
    resolve_section_assets,
)
from lectora_backend.pipeline.shared_utils.learning_objectives import (
    resolve_learning_objectives,
)

logger = logging.getLogger(__name__)

# Headings that represent structural/metadata sections — never content topics.
# They are rendered by A2 from metadata (description + learning_objectives), so
# they must not receive LLM-generated subtopics or be grouped as content parents.
_RESERVED_SECTION_RE = re.compile(
    r"^\s*(\d+(\.\d+)*\s+)?"  # optional leading "N.0 " prefix
    r"(overview|learning\s+objectives?|learning\s+outcomes?|course\s+objectives?|"
    r"summary|assessment|introduction)\s*$",
    re.IGNORECASE,
)


def _is_reserved_section(heading: str) -> bool:
    """Return True if *heading* names a structural section that must not hold subtopics."""
    return bool(_RESERVED_SECTION_RE.match(heading.strip()))


def _normalize_section_level(level: Any) -> int:
    """Clamp section levels into the schema-supported range (1..4)."""
    try:
        value = int(level)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(value, 4))


# -- State -------------------------------------------------------------------


class A1State(TypedDict):
    shared_state_path: str
    docx_path: str

    run_id: str
    a0_data: dict[str, Any]

    # Parser outputs (source of truth for structure)
    raw_sections: list[dict[str, Any]]
    total_word_count: int
    kc_count: int

    # Image mapping (no LLM)
    image_map: dict[str, Any]

    # LLM enrichment
    enrichment: dict[str, Any]

    # Final assembled spec
    course_spec: dict[str, Any]
    inconsistencies: list[dict[str, Any]]

    # Control flow
    retry_count: int
    status: str
    error: Optional[str]

    feedback: Optional[dict[str, Any]]


# -- Node: load_shared_state -------------------------------------------------


def load_shared_state(state: A1State) -> A1State:
    logger.info("[A1] Loading A0 shared state...")
    try:
        with open(state["shared_state_path"]) as f:
            data = json.load(f)
        resolved_los = resolve_learning_objectives(data)
        if resolved_los and not (data.get("extracted_inputs", {}) or {}).get("learning_objectives"):
            extracted_inputs = dict(data.get("extracted_inputs", {}) or {})
            extracted_inputs["learning_objectives"] = resolved_los
            data = {**data, "extracted_inputs": extracted_inputs}
            logger.info(
                "[A1] Backfilled %s learning objective(s) from llm_to_outline for PDF-only source.",
                len(resolved_los),
            )
        return {
            **state,
            "run_id": data["run_id"],
            "a0_data": data,
            "status": "running",
            "error": None,
        }
    except Exception as e:
        return {**state, "status": "failed", "error": f"load_shared_state: {e}"}


# -- Node: parse_document ----------------------------------------------------


def _append_section_body(current: dict, text: str) -> None:
    """Add a non-heading paragraph to the open section (KC lines included)."""
    current["paragraphs"].append(text)
    current["word_count"] += count_words(text)
    current["interactive_elements"] = collect_interactive_elements(
        [text],
        initial=current.get("interactive_elements", []),
    )


def _parse_pdf_sections_from_shared_state(
    a0_data: dict,
) -> tuple[list[dict], int, int]:
    """
    Reconstruct raw_sections for PDF source files using the structural data
    that A0 already persisted into shared_state.

    Uses:
      - ``extracted_inputs.heading_tree`` — section boundaries (level, text, para_idx)
      - ``extracted_inputs.indexed_content`` — ``[P<N>] …`` paragraph blocks

    Returns ``(sections, total_word_count, kc_count)``.
    """
    import re as _re

    extracted: dict = a0_data.get("extracted_inputs", {})
    heading_tree: list[dict] = extracted.get("heading_tree", [])
    indexed_content: str = extracted.get("indexed_content", "") or ""

    # Parse indexed_content into {para_idx: text} mapping
    para_map: dict[int, str] = {}
    for m in _re.finditer(r"\[P(\d+)\]\s*(.*?)(?=\[P\d+\]|\Z)", indexed_content, _re.DOTALL):
        idx = int(m.group(1))
        text = m.group(2).strip()
        if text:
            para_map[idx] = text

    max_para = max(para_map.keys(), default=0)

    if not heading_tree:
        # No heading structure — wrap all content in a single synthetic section
        all_paras = [para_map[k] for k in sorted(para_map.keys())]
        all_text = " ".join(all_paras)
        wc = count_words(all_text)
        interactive_elements = collect_interactive_elements(all_paras)
        return (
            [
                {
                    "id": "s1_content",
                    "heading": "Content",
                    "level": 1,
                    "is_knowledge_check": False,
                    "has_knowledge_check": False,
                    "para_start": 0,
                    "para_end": max_para,
                    "paragraphs": all_paras,
                    "word_count": wc,
                    "interactive_elements": interactive_elements,
                }
            ],
            wc,
            0,
        )

    sections: list[dict] = []
    kc_count = 0

    for i, h in enumerate(heading_tree):
        para_start: int = h.get("para_idx", 0)
        para_end: int = (
            heading_tree[i + 1].get("para_idx", para_start) - 1
            if i + 1 < len(heading_tree)
            else max_para
        )
        level: int = _normalize_section_level(h.get("level", 1))
        heading_text: str = h.get("text", "")
        is_kc = "Knowledge Check" in heading_text and level == 3

        # Body paragraphs (everything after the heading line itself)
        body_paras = [
            para_map[j]
            for j in range(para_start + 1, para_end + 1)
            if j in para_map
        ]
        wc = count_words(" ".join(body_paras))
        interactive_elements = collect_interactive_elements(body_paras)

        section: dict = {
            "id": f"s{len(sections)+1}_{to_snake(heading_text)}",
            "heading": heading_text,
            "level": level,
            "is_knowledge_check": is_kc,
            "has_knowledge_check": False,
            "para_start": para_start,
            "para_end": para_end,
            "paragraphs": body_paras,
            "word_count": wc,
            "interactive_elements": interactive_elements,
        }

        if is_kc and sections:
            sections[-1]["has_knowledge_check"] = True
            kc_count += 1

        sections.append(section)

    total_words = sum(s["word_count"] for s in sections)
    return sections, total_words, kc_count


def parse_document(state: A1State) -> A1State:
    if state["status"] == "failed":
        return state

    attempt = state.get("retry_count", 0) + 1
    logger.info("[A1] Parsing document (attempt %s)...", attempt)

    try:
        import os as _os
        docx_path = state["docx_path"]

        # ── PDF source: reconstruct sections from A0's shared-state structural data ──
        if docx_path.lower().endswith(".pdf"):
            logger.info(
                "[A1] PDF source detected — rebuilding sections from "
                "shared-state heading_tree + indexed_content."
            )
            a0_data = state.get("a0_data", {})
            sections, total_words, kc_count = _parse_pdf_sections_from_shared_state(a0_data)
            logger.info(
                "[A1] Reconstructed %s sections, %s words, %s KC(s) from PDF shared state.",
                len(sections),
                total_words,
                kc_count,
            )
            return {
                **state,
                "raw_sections": sections,
                "total_word_count": total_words,
                "kc_count": kc_count,
                "error": None,
            }

        # ── DOCX source: original python-docx parsing path ──────────────────────────
        if not _os.path.exists(docx_path):
            raise FileNotFoundError(f"Source document not found: {docx_path!r}")
        doc = Document(docx_path)
        all_paras = doc.paragraphs
        sections: list[dict] = []
        current: dict | None = None
        kc_count = 0

        for para_idx, p in enumerate(all_paras):
            style = p.style.name
            text = p.text.strip()
            if not text:
                continue

            if style in ("Heading 1", "Heading 2", "Heading 3"):
                level = int(style[-1])
                is_kc = "Knowledge Check" in text and level == 3

                if is_kc and current is not None:
                    # Fold KC into the preceding section — not a real heading for
                    # para_start/para_end (range is only bounded by the next section
                    # heading or EOF).
                    current["has_knowledge_check"] = True
                    kc_count += 1
                    _append_section_body(current, text)
                    continue

                if current is not None:
                    current["para_end"] = para_idx - 1
                    sections.append(current)

                current = {
                    "id": f"s{len(sections)+1}_{to_snake(text)}",
                    "heading": text,
                    "level": level,
                    "is_knowledge_check": False,
                    "has_knowledge_check": False,
                    # Span: this heading through the paragraph before the next
                    # heading (para_end set when that heading is seen, or EOF).
                    "para_start": para_idx,
                    "para_end": para_idx,
                    "paragraphs": [],
                    "word_count": 0,
                    "interactive_elements": [],
                }
            elif current is not None:
                _append_section_body(current, text)

        if current is not None:
            current["para_end"] = len(all_paras) - 1
            sections.append(current)

        total_words = sum(s["word_count"] for s in sections)

        logger.info(
            "[A1] Parsed %s sections, %s words, %s knowledge checks.",
            len(sections),
            total_words,
            kc_count,
        )

        return {
            **state,
            "raw_sections": sections,
            "total_word_count": total_words,
            "kc_count": kc_count,
            "error": None,
        }
    except Exception as e:
        rc = state.get("retry_count", 0)
        return {
            **state,
            "retry_count": rc + 1,
            "error": f"parse_document: {e}",
            "status": "running" if rc < 1 else "failed",
        }


# -- Node: validate_los ------------------------------------------------------


def validate_los(state: A1State) -> A1State:
    if state["status"] in ("failed", "stopped"):
        return state

    logger.info("[A1] Validating learning objectives...")
    los = state["a0_data"].get("extracted_inputs", {}).get("learning_objectives", [])

    if not los:
        sections = state.get("raw_sections", [])
        if sections:
            logger.warning(
                "[A1] No learning objectives found — continuing without LOs (sections present)."
            )
            return {
                **state,
                "status": "complete",
                "error": "Missing LOs — proceeding without them",
            }
        else:
            logger.error("[A1] CRITICAL — no learning objectives and no sections. Stopping pipeline.")
            return {
                **state,
                "status": "stopped",
                "error": "No sections and no LOs — cannot proceed",
            }

    logger.info("[A1] %s learning objectives confirmed.", len(los))
    return state


# -- Node: map_images --------------------------------------------------------


def map_images(state: A1State) -> A1State:
    if state["status"] in ("failed", "stopped"):
        return state

    images: list[dict] = state["a0_data"].get("images", [])
    if not images:
        logger.info("[A1] No images in shared state — skipping image mapping.")
        return {**state, "image_map": {}}

    logger.info(
        "[A1] Mapping %s images to sections by paragraph index...", len(images)
    )

    image_map = map_images_to_sections(images, state["raw_sections"])

    placed = sum(len(v) for k, v in image_map.items() if k != "unassigned")
    logger.info(
        "[A1] Mapped %s/%s images. Unassigned: %s",
        placed,
        len(images),
        len(image_map["unassigned"]),
    )

    return {**state, "image_map": image_map}


# -- Node: enrich_with_llm ---------------------------------------------------


def enrich_with_llm(state: A1State) -> A1State:
    if state["status"] in ("failed", "stopped"):
        return state

    logger.info("[A1] Enriching sections with AzureOpenAI (subtopics + LO mapping)...")
    los = state["a0_data"].get("extracted_inputs", {}).get("learning_objectives", [])

    section_input = {}
    for s in state["raw_sections"]:
        if _is_reserved_section(s["heading"]):
            # Reserved structural sections (Overview, LO, Summary, Assessment) are
            # rendered by A2 from metadata — they must not receive content subtopics.
            continue
        preview = " ".join(s["paragraphs"][:2])[:250] if s["paragraphs"] else ""
        section_input[s["heading"]] = {"preview": preview}

    payload: dict = {
        "learning_objectives": {str(i): lo for i, lo in enumerate(los)},
        "sections": section_input,
    }
    fb = state.get("feedback")
    if fb:
        vf = fb.get("validator_feedback")
        if vf:
            payload["validator_feedback"] = vf
        att = fb.get("attempt")
        if att is not None:
            payload["retry_attempt"] = att

    try:
        raw = chat(ENRICH_SYSTEM, json.dumps(payload, ensure_ascii=False))
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        enrichment = json.loads(raw)
        logger.info("[A1] LLM enriched %s sections.", len(enrichment))
        return {**state, "enrichment": enrichment, "error": None}
    except Exception as e:
        logger.warning("[A1] LLM enrichment failed: %s — continuing without enrichment.", e)
        return {**state, "enrichment": {}, "error": f"enrich_with_llm failed: {e}"}


# -- Node: build_course_spec -------------------------------------------------


def build_course_spec(state: A1State) -> A1State:
    """
    Assemble ``course_spec`` from ``raw_sections`` (parse_document) + LLM enrich.

    Paragraph span (``para_start`` / ``para_end``) follows the parser contract:
    inclusive DOCX paragraph indices from this section's **heading** through the
    paragraph immediately **before** the next real H1/H2/H3 heading; Knowledge
    Check lines are **body** (inside the span), not a boundary.
    """
    if state["status"] in ("failed", "stopped"):
        return state

    logger.info("[A1] Assembling course_spec from parsed + enriched data...")
    enrichment = state.get("enrichment", {})
    sections_out = []

    # Pacing — derive wpm from the active rule pack (falls back to 180).
    rule_family = (
        state["a0_data"].get("request_spec", {})
        .get("rule_classification", {})
        .get("family")
    )
    rule_pack = resolve_rule_pack(rule_family) if rule_family else None
    wpm = wpm_from_rule_pack(rule_pack or {}, default=180)
    logger.info("[A1] Pacing: %s words/min (derived from rule pack)", wpm)

    for s in state["raw_sections"]:
        heading = s["heading"]
        enrich = enrichment.get(heading, {})

        para_start = s["para_start"]
        para_end = max(s["para_end"], para_start)
        level = _normalize_section_level(s["level"])

        mapped_images = [
            im
            for im in state.get("image_map", {}).get(s["id"], [])
            if para_start <= im.get("para_idx", -1) <= para_end
        ]
        raw_ies, section_images = resolve_section_assets(
            s.get("interactive_elements", []),
            mapped_images,
            has_knowledge_check=bool(s.get("has_knowledge_check")),
        )

        # KC: only set has_knowledge_check True if IE confirms it.
        has_kc_final = "knowledge_check" in raw_ies

        wc = s.get("word_count", 0) or 0
        sections_out.append(
            {
                "id": s["id"],
                "heading": heading,
                "level": level,
                "is_reserved": _is_reserved_section(heading),
                "is_knowledge_check": s["is_knowledge_check"],
                "has_knowledge_check": has_kc_final,
                "para_start": para_start,
                "para_end": para_end,
                "word_count": wc,
                "estimated_duration_minutes": round(words_to_minutes(wc, wpm=wpm), 2) if wc else 0.0,
                "interactive_elements": list(raw_ies),
                # best-effort: if enrichment returns mapped objective indices, include them
                "maps_to_objectives": enrich.get("maps_to_objectives", []),
                "images": section_images,
                "image_count": len(section_images),
            }
        )

    a0_inputs = state["a0_data"].get("extracted_inputs", {})
    course_spec = {
        "run_id": state["run_id"],
        "course_id": a0_inputs.get("course_id"),
        "course_title": a0_inputs.get("title"),
        "extracted_inputs": {
            "title": a0_inputs.get("title"),
            "course_id": a0_inputs.get("course_id"),
            "learning_objectives": a0_inputs.get("learning_objectives", []),
        },
        "sections": sections_out,
    }

    logger.info("[A1] course_spec built: %s sections.", len(sections_out))
    return {**state, "course_spec": course_spec}


# -- Node: detect_inconsistencies -------------------------------------------


def detect_inconsistencies(state: A1State) -> A1State:
    if state["status"] in ("failed", "stopped"):
        return state

    logger.info("[A1] Checking for inconsistencies...")
    issues = []
    spec = state["course_spec"]
    request_spec = state["a0_data"].get("request_spec", {})
    rules = {}
    los = state["a0_data"].get("extracted_inputs", {}).get("learning_objectives", [])

    # 1 — KC count vs rule pack minimum
    kc_found = state["kc_count"]
    min_kc = rules.get("min_kc_total", 0)
    if kc_found < min_kc:
        issues.append(
            {
                "field": "knowledge_check_count",
                "expected": f">= {min_kc}",
                "found": kc_found,
                "severity": "warning",
                "message": (
                    f"Only {kc_found} knowledge check heading(s) found; "
                    f"rule pack requires at least {min_kc}."
                ),
            }
        )

    # 2 — LO coverage gaps
    mapped = set()
    for s in spec.get("sections", []):
        mapped.update(s.get("maps_to_objectives", []))
    unmapped = [i for i in range(len(los)) if i not in mapped]
    if unmapped:
        issues.append(
            {
                "field": "learning_objectives_coverage",
                "expected": f"all {len(los)} LOs mapped",
                "found": f"LO indices {unmapped} unmapped",
                "severity": "info",
                "message": (
                    f"LO(s) {[i+1 for i in unmapped]} have no explicit section mapping. "
                    "May need A2 to address coverage gaps."
                ),
            }
        )

    if issues:
        for iss in issues:
            logger.info(
                "  [%s] %s: %s",
                iss["severity"].upper(),
                iss["field"],
                iss["message"],
            )
    else:
        logger.info("[A1] No inconsistencies detected.")

    return {**state, "inconsistencies": issues}


# -- Node: persist_output ----------------------------------------------------


def persist_output(state: A1State) -> A1State:
    if state["status"] in ("failed", "stopped"):
        return state

    logger.info("[A1] Persisting to shared state...")

    a1_output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "course_spec": state["course_spec"],
        "inconsistencies": state.get("inconsistencies", []),
    }

    with open(state["shared_state_path"]) as f:
        shared = json.load(f)
    shared["agent_outputs"]["A1"] = a1_output
    shared["status"] = "A1_complete"
    # Write atomically: write to a temp file then replace, so readers never see a partial file
    import os as _os
    _tmp_path = state["shared_state_path"] + ".tmp"
    with open(_tmp_path, "w") as f:
        json.dump(shared, f, indent=2, default=str)
    _os.replace(_tmp_path, state["shared_state_path"])

    output_dir = Path(state["shared_state_path"]).expanduser().resolve().parent
    spec_path = output_dir / "course_spec.json"
    with open(spec_path, "w") as f:
        json.dump(a1_output, f, indent=2, default=str)

    logger.info("[A1] course_spec written -> %s", spec_path)
    return {**state, "status": "complete"}


# -- Terminal nodes ----------------------------------------------------------


def _write_terminal(state: A1State, label: str) -> None:
    output_dir = Path(state["shared_state_path"]).expanduser().resolve().parent
    path = output_dir / f"a1_{label}.json"
    with open(path, "w") as f:
        json.dump(
            {
                "status": label.upper(),
                "reason": state.get("error"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            f,
            indent=2,
        )


def failed_end(state: A1State) -> A1State:
    logger.error("[A1] FAILED: %s", state.get("error"))
    _write_terminal(state, "failed")
    return {**state, "status": "failed"}


def stopped_end(state: A1State) -> A1State:
    logger.warning("[A1] STOPPED: %s", state.get("error"))
    _write_terminal(state, "stopped")
    return {**state, "status": "stopped"}


# -- Routing -----------------------------------------------------------------


def route_after_load(state: A1State) -> str:
    return "failed_end" if state["status"] == "failed" else "parse_document"


def route_after_parse(state: A1State) -> str:
    if state["status"] == "failed":
        return "failed_end"
    if state.get("error") and state.get("retry_count", 0) <= 1:
        return "parse_document"
    return "validate_los"


def route_after_validate(state: A1State) -> str:
    return "stopped_end" if state["status"] == "stopped" else "map_images"


def route_after_build(state: A1State) -> str:
    return "failed_end" if state["status"] == "failed" else "detect_inconsistencies"


# -- Build graph -------------------------------------------------------------


def build_graph():
    g = StateGraph(A1State)

    for name, fn in [
        ("load_shared_state", load_shared_state),
        ("parse_document", parse_document),
        ("validate_los", validate_los),
        ("map_images", map_images),
        ("enrich_with_llm", enrich_with_llm),
        ("build_course_spec", build_course_spec),
        ("detect_inconsistencies", detect_inconsistencies),
        ("persist_output", persist_output),
        ("failed_end", failed_end),
        ("stopped_end", stopped_end),
    ]:
        g.add_node(name, fn)

    g.set_entry_point("load_shared_state")

    g.add_conditional_edges(
        "load_shared_state",
        route_after_load,
        {"parse_document": "parse_document", "failed_end": "failed_end"},
    )
    g.add_conditional_edges(
        "parse_document",
        route_after_parse,
        {
            "parse_document": "parse_document",
            "validate_los": "validate_los",
            "failed_end": "failed_end",
        },
    )
    g.add_conditional_edges(
        "validate_los",
        route_after_validate,
        {"map_images": "map_images", "stopped_end": "stopped_end"},
    )
    g.add_edge("map_images", "enrich_with_llm")
    g.add_edge("enrich_with_llm", "build_course_spec")
    g.add_conditional_edges(
        "build_course_spec",
        route_after_build,
        {
            "detect_inconsistencies": "detect_inconsistencies",
            "failed_end": "failed_end",
        },
    )
    g.add_edge("detect_inconsistencies", "persist_output")
    g.add_edge("persist_output", END)
    g.add_edge("failed_end", END)
    g.add_edge("stopped_end", END)

    return g.compile()


# -- Public API (used by pipeline.py) ----------------------------------------


def run(
    shared_state_path: str,
    docx_path: str,
    feedback: dict[str, Any] | None = None,
) -> A1Output:
    """
    Run the A1 LangGraph and return a typed A1Output.

    The internal LangGraph state (A1State) is kept as a TypedDict for LangGraph
    compatibility; only the public return value is converted to a Pydantic model.
    """
    app = build_graph()
    initial: A1State = {
        "shared_state_path": shared_state_path,
        "docx_path": docx_path,
        "run_id": "",
        "a0_data": {},
        "raw_sections": [],
        "total_word_count": 0,
        "kc_count": 0,
        "image_map": {},
        "enrichment": {},
        "course_spec": {},
        "inconsistencies": [],
        "retry_count": 0,
        "status": "running",
        "error": None,
        "feedback": feedback,
    }
    final: A1State = app.invoke(initial)

    if final["status"] == "complete":
        course_spec = CourseSpec.model_validate(final["course_spec"])
        inconsistencies = [
            Inconsistency.model_validate(i) for i in final.get("inconsistencies", [])
        ]
        return A1Output(
            status=A1Status.complete,
            course_spec=course_spec,
            inconsistencies=inconsistencies,
            retry_count=final.get("retry_count", 0),
            timestamp=datetime.now(timezone.utc),
        )

    return A1Output(
        status=A1Status.failed,
        error=final.get("error"),
        retry_count=final.get("retry_count", 0),
        timestamp=datetime.now(timezone.utc),
    )
