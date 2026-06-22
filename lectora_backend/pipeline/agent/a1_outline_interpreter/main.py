"""
A1 — Timed Outline Interpreter

Backward-compatibility shim. Implementation lives in orchestrator/graph.py
and the step_0N_* subdirectories.
"""
from .orchestrator.graph import build_graph, run  # noqa: F401
from .shared.helpers.section_helpers import _normalize_section_level  # noqa: F401
from .step_02_parse_document.utils.pdf_parser import _parse_pdf_sections_from_shared_state  # noqa: F401

__all__ = ["run", "build_graph", "_normalize_section_level", "_parse_pdf_sections_from_shared_state"]
