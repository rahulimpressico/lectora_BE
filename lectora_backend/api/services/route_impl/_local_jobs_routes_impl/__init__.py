from __future__ import annotations

from . import base as _base
from . import helpers_core as _helpers_core
from . import helpers_ai_ops as _helpers_ai_ops
from . import models_payloads as _models_payloads
from . import pipeline_runner as _pipeline_runner
from . import pipeline_create_job as _pipeline_create_job
from . import job_resolution as _job_resolution
from . import events_and_course as _events_and_course
from . import editor_routes as _editor_routes
from . import ai_and_artifacts as _ai_and_artifacts

_MODULES = [
    _base,
    _helpers_core,
    _helpers_ai_ops,
    _models_payloads,
    _pipeline_runner,
    _pipeline_create_job,
    _job_resolution,
    _events_and_course,
    _editor_routes,
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
