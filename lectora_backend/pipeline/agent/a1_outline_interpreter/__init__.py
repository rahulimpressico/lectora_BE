"""A1 — Timed Outline Interpreter: enriches the structured outline with LLM-generated subtopics and LO mappings."""

from .main import build_graph, run

__all__ = ["run", "build_graph"]
