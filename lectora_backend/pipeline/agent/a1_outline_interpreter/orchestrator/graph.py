"""
A1 — LangGraph orchestrator.

Wires node functions from each step into the LangGraph StateGraph.
All business logic lives in the step_0N_* subdirectories.
"""
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import StateGraph, END

from ..shared.models.state import A1State
from ..step_01_load_state.utils.loader import load_shared_state
from ..step_02_parse_document.utils.parser import parse_document
from ..step_02_parse_document.utils.pdf_parser import _parse_pdf_sections_from_shared_state
from ..step_03_validate.utils.lo_validator import validate_los
from ..step_04_map_images.utils.image_mapper import map_images
from ..step_05_enrich.utils.enricher import enrich_with_llm
from ..step_06_build_spec.utils.spec_builder import build_course_spec
from ..step_07_detect_issues.utils.detector import detect_inconsistencies
from ..step_08_persist.utils.writer import persist_output, failed_end, stopped_end

from lectora_backend.pipeline.models import A1Output, A1Status, CourseSpec, Inconsistency


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


# -- Graph -------------------------------------------------------------------

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
        "load_shared_state", route_after_load,
        {"parse_document": "parse_document", "failed_end": "failed_end"},
    )
    g.add_conditional_edges(
        "parse_document", route_after_parse,
        {"parse_document": "parse_document", "validate_los": "validate_los", "failed_end": "failed_end"},
    )
    g.add_conditional_edges(
        "validate_los", route_after_validate,
        {"map_images": "map_images", "stopped_end": "stopped_end"},
    )
    g.add_edge("map_images", "enrich_with_llm")
    g.add_edge("enrich_with_llm", "build_course_spec")
    g.add_conditional_edges(
        "build_course_spec", route_after_build,
        {"detect_inconsistencies": "detect_inconsistencies", "failed_end": "failed_end"},
    )
    g.add_edge("detect_inconsistencies", "persist_output")
    g.add_edge("persist_output", END)
    g.add_edge("failed_end", END)
    g.add_edge("stopped_end", END)

    return g.compile()


# -- Public API --------------------------------------------------------------

def run(
    shared_state_path: str,
    docx_path: str,
    feedback: dict[str, Any] | None = None,
) -> A1Output:
    """Run the A1 LangGraph and return a typed A1Output."""
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
        inconsistencies = [Inconsistency.model_validate(i) for i in final.get("inconsistencies", [])]
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
