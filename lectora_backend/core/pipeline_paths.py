"""Canonical on-disk paths for local pipeline artifacts."""
from __future__ import annotations

from pathlib import Path

_LECTORA_BACKEND_ROOT = Path(__file__).resolve().parents[1]

PIPELINE_DIR = _LECTORA_BACKEND_ROOT / "pipeline"
PIPELINE_COURSES_DIR = PIPELINE_DIR / "courses"
PIPELINE_SHARED_STATE_DIR = PIPELINE_DIR / "shared_state"
