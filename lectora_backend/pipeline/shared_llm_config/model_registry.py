"""
Model Registry — centralised source of truth for per-agent LLM deployments.

Agents call `get_deployment(agent_id)` at *call time* so any override written
via the settings API is picked up by the next generation run without a restart.

Overrides are persisted to ``model_overrides.json`` next to this file.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_OVERRIDES_FILE = Path(__file__).parent / "model_overrides.json"
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Defaults — the built-in deployment for each LLM-using agent
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, str] = {
    "A0": "o3",
    "A1": "gpt-5.4-mini",
    "A2": "gpt-5.4",
}

# ---------------------------------------------------------------------------
# Available models exposed to the UI
# ---------------------------------------------------------------------------

AVAILABLE_MODELS: list[dict] = [
    {"id": "o3",           "label": "o3",            "provider": "Azure OpenAI", "tier": "reasoning"},
    {"id": "o4-mini",      "label": "o4 Mini",       "provider": "Azure OpenAI", "tier": "reasoning"},
    {"id": "gpt-5.4",      "label": "GPT-5.4",       "provider": "Azure OpenAI", "tier": "flagship"},
    {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini",  "provider": "Azure OpenAI", "tier": "efficient"},
    {"id": "gpt-4o",       "label": "GPT-4o",         "provider": "Azure OpenAI", "tier": "previous"},
    {"id": "gpt-4o-mini",  "label": "GPT-4o Mini",    "provider": "Azure OpenAI", "tier": "previous"},
]

# ---------------------------------------------------------------------------
# Agent metadata
# ---------------------------------------------------------------------------

AGENT_META: dict[str, dict] = {
    "A0": {
        "name": "Request Synthesizer",
        "role": "Extracts metadata, classifies rule family, generates & parses timed outline",
        "pipeline_step": 1,
        "supports_temperature": False,
    },
    "A1": {
        "name": "Outline Interpreter",
        "role": "Parses document structure, enriches sections, builds course spec",
        "pipeline_step": 2,
        "supports_temperature": True,
    },
    "A2": {
        "name": "Content Generator",
        "role": "Generates course content per lesson, descriptions, and conclusions",
        "pipeline_step": 3,
        "supports_temperature": True,
    },
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_overrides() -> dict[str, str]:
    with _lock:
        if not _OVERRIDES_FILE.exists():
            return {}
        try:
            return json.loads(_OVERRIDES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}


def _write_overrides(overrides: dict[str, str]) -> None:
    with _lock:
        _OVERRIDES_FILE.write_text(
            json.dumps(overrides, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_deployment(agent_id: str) -> str:
    """Return the effective deployment for *agent_id*, respecting any override."""
    overrides = _read_overrides()
    return overrides.get(agent_id) or DEFAULTS.get(agent_id, "gpt-5.4")


def get_all_configs() -> list[dict]:
    """Return per-agent configs for the settings endpoint."""
    overrides = _read_overrides()
    return [
        {
            "agent_id": agent_id,
            "name": meta["name"],
            "role": meta["role"],
            "pipeline_step": meta["pipeline_step"],
            "default_deployment": DEFAULTS[agent_id],
            "current_deployment": overrides.get(agent_id) or DEFAULTS[agent_id],
            "is_overridden": agent_id in overrides,
            "supports_temperature": meta["supports_temperature"],
        }
        for agent_id, meta in AGENT_META.items()
    ]


def set_deployment(agent_id: str, deployment: str) -> None:
    """Persist a deployment override for one agent."""
    overrides = _read_overrides()
    overrides[agent_id] = deployment
    _write_overrides(overrides)


def reset_deployment(agent_id: str) -> None:
    """Remove the override for *agent_id*, reverting to the default."""
    overrides = _read_overrides()
    overrides.pop(agent_id, None)
    _write_overrides(overrides)


def reset_all_deployments() -> None:
    """Remove every override, reverting all agents to their defaults."""
    _write_overrides({})
