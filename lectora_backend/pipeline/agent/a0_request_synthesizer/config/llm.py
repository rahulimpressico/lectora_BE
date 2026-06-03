"""
LLM config for A0 — Request Synthesizer.

Uses DynamicLLMConfig so the deployment is resolved from model_registry at
every call. Changes made via the settings API take effect immediately without
restarting the server.

Two separate configs:
  A0        → classification (o3 by default — reasoning model, small payload)
  A0_TO     → TO generation  (gpt-5.4 by default — large context for DOCX+PDF text)
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

    @property
    def deployment(self) -> str:  # type: ignore[override]
        return get_deployment("A0")


class _DynamicTOConfig:
    """Proxy for TO generation — uses A0_TO registry key (gpt-5.4 default)."""

    temperature: float | None = None
    max_tokens: int | None = None
    top_k: int | None = None

    @property
    def deployment(self) -> str:  # type: ignore[override]
        return get_deployment("A0_TO")


# Module-level singletons
AGENT_CONFIG: LLMConfig = _DynamicConfig()  # type: ignore[assignment]
AGENT_TO_CONFIG: LLMConfig = _DynamicTOConfig()  # type: ignore[assignment]


# ── Pre-configured chat wrappers ─────────────────────────────────────────────

def chat(system_prompt: str, user_msg: str) -> str:
    """Classification call — uses A0 (o3) for rule-family classification."""
    return _chat(system_prompt, user_msg, config=AGENT_CONFIG, agent="A0")


def chat_for_to(system_prompt: str, user_msg: str) -> str:
    """TO generation call — uses A0_TO (gpt-5.4) for large DOCX+PDF context."""
    return _chat(system_prompt, user_msg, config=AGENT_TO_CONFIG, agent="A0_TO")
