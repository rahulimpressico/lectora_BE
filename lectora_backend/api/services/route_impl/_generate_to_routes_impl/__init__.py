from __future__ import annotations

from . import base as _base
from . import helpers_core as _helpers_core
from . import helpers_pipeline as _helpers_pipeline
from . import helpers_storage as _helpers_storage
from . import endpoints_upload_analysis as _endpoints_upload_analysis
from . import endpoints_generation as _endpoints_generation
from . import endpoints_to_ops as _endpoints_to_ops

_MODULES = [
    _base,
    _helpers_core,
    _helpers_pipeline,
    _helpers_storage,
    _endpoints_upload_analysis,
    _endpoints_generation,
    _endpoints_to_ops,
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


def _has_route(path: str, method: str) -> bool:
    for _r in router.routes:
        if getattr(_r, "path", None) == path and method in (getattr(_r, "methods", set()) or set()):
            return True
    return False


def _ensure_route(path: str, endpoint, method: str) -> None:
    if _has_route(path, method):
        return
    router.add_api_route(path, endpoint, methods=[method])


_ensure_route("/upload", upload_document, "POST")
_ensure_route("/ingestion-status/{document_id}", get_ingestion_status, "GET")
_ensure_route("/analyze-source", analyze_source, "POST")
_ensure_route("/generate-to", generate_to, "POST")
_ensure_route("/generate-to/jobs", list_generate_to_jobs, "GET")
_ensure_route("/generate-to/jobs/{job_id}/cancel", cancel_generate_to_job, "POST")
_ensure_route("/load-to", load_to_from_path, "GET")
_ensure_route("/save-to", save_to, "POST")
_ensure_route("/generate-learning-objectives", generate_learning_objectives, "POST")
_ensure_route("/suggest-outline-structure", suggest_outline_structure, "POST")
_ensure_route("/suggest-course-type", suggest_course_type, "POST")
_ensure_route("/generate-to/jobs/{job_id}", get_generate_to_job, "GET")
_ensure_route("/revise-to", revise_to, "POST")

__all__ = [name for name in globals() if not name.startswith("__")]
