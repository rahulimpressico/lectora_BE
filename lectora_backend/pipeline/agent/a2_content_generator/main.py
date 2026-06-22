"""
A2 — Content Generator
Backward-compatibility shim. Implementation lives in orchestrator/generator.py.
"""

from .orchestrator.generator import (  # noqa: F401
    A2ContentGenerator,
    render_study_guide_from_state,
    _build_course_description,
    _build_course_conclusion,
)

__all__ = [
    "A2ContentGenerator",
    "render_study_guide_from_state",
    "_build_course_description",
    "_build_course_conclusion",
]
