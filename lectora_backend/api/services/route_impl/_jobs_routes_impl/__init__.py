from __future__ import annotations

from . import base as _base
from . import helpers_mapping as _helpers_mapping
from . import endpoints_jobs as _endpoints_jobs
from . import course_content as _course_content
from . import ai_and_artifacts as _ai_and_artifacts

_MODULES = [
    _base,
    _helpers_mapping,
    _endpoints_jobs,
    _course_content,
    _ai_and_artifacts,
]

# Ensure functions keep cross-module symbol resolution identical.
for _module in _MODULES:
    for _other in _MODULES:
        if _other is _module:
            continue
        for _name, _value in _other.__dict__.items():
            if _name.startswith("__"):
                continue
            _module.__dict__.setdefault(_name, _value)

for _module in _MODULES:
    for _name, _value in _module.__dict__.items():
        if _name.startswith("__"):
            continue
        globals().setdefault(_name, _value)

__all__ = [name for name in globals() if not name.startswith("__")]
