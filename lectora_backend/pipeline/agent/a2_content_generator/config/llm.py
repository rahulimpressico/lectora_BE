"""
LLM config for A2 — Content Generator.

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
    deployment="gpt-5.4",
    temperature=0.7,
)

# ── Course description LLM config (separate call, lower temperature) ─────────
COURSE_DESCRIPTION_CONFIG = LLMConfig(
    deployment=AGENT_CONFIG.deployment,
    temperature=0.35,
    max_tokens=1200,
)

CONCLUSION_CONFIG = LLMConfig(
    deployment=AGENT_CONFIG.deployment,
    temperature=0.35,
    max_tokens=1500,
)


# ── Pre-configured chat wrapper ─────────────────────────────────────────────


def chat(system_prompt: str, user_msg: str) -> str:
    """Call AzureOpenAI with A2's settings."""
    return _chat(system_prompt, user_msg, config=AGENT_CONFIG, agent="A2")
