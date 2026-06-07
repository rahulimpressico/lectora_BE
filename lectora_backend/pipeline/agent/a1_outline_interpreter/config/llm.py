"""
LLM config for A1 — Timed Outline Interpreter.

Uses DynamicLLMConfig so the deployment is resolved from model_registry at
every call. Changes made via the settings API take effect immediately without
restarting the server.
"""

from lectora_backend.pipeline.shared_llm_config.llm import (
    LLMConfig,
    chat as _chat,
    get_client,
)
from lectora_backend.pipeline.shared_llm_config.model_registry import get_deployment


class _DynamicConfig:
    """Proxy that reads `deployment` from the registry on every attribute access."""

    temperature: float | None = None
    max_tokens: int | None = None
    top_k: int | None = None
    response_format: dict | None = None

    @property
    def deployment(self) -> str:  # type: ignore[override]
        return get_deployment("A1")


# Module-level singleton — stays compatible with any code that imports AGENT_CONFIG
AGENT_CONFIG: LLMConfig = _DynamicConfig()  # type: ignore[assignment]


# ── Pre-configured chat wrapper ─────────────────────────────────────────────

def chat(system_prompt: str, user_msg: str) -> str:
    """Call AzureOpenAI with A1's current (registry-resolved) settings."""
    return _chat(system_prompt, user_msg, config=AGENT_CONFIG, agent="A1")
