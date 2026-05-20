"""
LLM config for A1 — Timed Outline Interpreter.

Sets agent-specific model settings and re-exports a pre-configured chat().
All Azure connection logic lives in shared_llm_config/llm.py.
"""

from lectora_backend.pipeline.shared_llm_config.llm import (
    LLMConfig,
    chat as _chat,
    get_client,
)

# ── Agent-specific settings ─────────────────────────────────────────────────
# Change ONLY these values per agent. Do NOT put deployment in .env.

AGENT_CONFIG = LLMConfig(
    deployment="gpt-5.4-mini",
)


# ── Pre-configured chat wrapper ─────────────────────────────────────────────


def chat(system_prompt: str, user_msg: str) -> str:
    """Call AzureOpenAI with A1's settings."""
    return _chat(system_prompt, user_msg, config=AGENT_CONFIG, agent="A1")
