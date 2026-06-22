"""
Section Mapper — maps Timed Outline sections to enriched course_spec groupings.
Backward-compatibility shim. Implementation lives in orchestrator/runner.py
and step_01_map_sections/.
"""

from .orchestrator.runner import run  # noqa: F401
from .step_01_map_sections.utils.mapper import map_sections  # noqa: F401

__all__ = ["run", "map_sections"]
