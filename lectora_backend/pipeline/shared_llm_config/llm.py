"""
Shared Azure OpenAI LLM Client
───────────────────────────────
Single reusable module for all pipeline agents (A0, A1, A2, S1, S2).

Every parameter (deployment, system_prompt, user_msg, temperature, max_tokens,
top_k, …) is fully dynamic — agents pass what they need via ``LLMConfig``.

Usage (from any agent's config/llm.py):
    from lectora_backend.pipeline.shared_llm_config.llm import chat, LLMConfig

    AGENT_CONFIG = LLMConfig(deployment="gpt-5.4-mini", temperature=0, max_tokens=4096)

    def chat_agent(system_prompt: str, user_msg: str) -> str:
        return chat(system_prompt, user_msg, config=AGENT_CONFIG)
"""

import time
from dataclasses import dataclass

from openai import AzureOpenAI

from lectora_backend.config import settings
from lectora_backend.pipeline.shared_llm_config.tracer import LLMTrace, write_trace


# ---------------------------------------------------------------------------
# Connection — read once from environment
# ---------------------------------------------------------------------------

_API_VERSION: str = "2024-12-01-preview"


# ---------------------------------------------------------------------------
# LLMConfig — agent-specific settings bundled in one place
# ---------------------------------------------------------------------------


@dataclass
class LLMConfig:
    """
    Holds all per-agent model settings.

    Attributes:
        deployment:     Azure deployment name (agent-specific, NOT from .env).
        temperature:    Passed through when set (default ``None`` = API default).
        max_tokens:     Max completion tokens when set.
        top_k:          Sampling top-k when set (e.g. ``1`` for greedy-ish decoding).
        response_format: When set, passed directly to the API (e.g.
                         ``{"type": "json_object"}`` to enforce JSON output).
                         Only supported by non-o-series models (gpt-4o, gpt-5.x, etc.).
                         Do NOT set on o3/o1 deployments — they will raise an API error.
    """

    deployment: str
    temperature: float | None = None
    max_tokens: int | None = None
    top_k: int | None = None
    response_format: dict | None = None


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def get_client() -> AzureOpenAI:
    """Return a configured AzureOpenAI client from environment variables."""
    if not settings.azure_openai_api_key:
        raise RuntimeError("azure_openai_api_key is not configured.")
    if not settings.azure_openai_endpoint:
        raise RuntimeError("azure_openai_endpoint is not configured.")

    return AzureOpenAI(
        api_key=settings.azure_openai_api_key,
        api_version=_API_VERSION,
        azure_endpoint=settings.azure_openai_endpoint,
    )


# ---------------------------------------------------------------------------
# Core chat function — fully dynamic
# ---------------------------------------------------------------------------


def chat(
    system_prompt: str,
    user_msg: str,
    config: LLMConfig,
    agent: str = "",
) -> str:
    """
    Send a system + user turn to Azure OpenAI and return the response text.

    Args:
        system_prompt: Instruction/persona for the model.
        user_msg:      The user-turn content (question, data, etc.).
        config:        LLMConfig with deployment, temperature, max_tokens.
        agent:         Agent label for tracing (e.g. "A0", "A1", "A2", "S1", "S2").

    Returns:
        Raw string content from the model's first choice.

    Raises:
        RuntimeError: If Azure credentials are not configured.
        openai.OpenAIError: Propagates API-level errors to the caller.
    """
    client = get_client()
    t_start = time.perf_counter()
    error_msg: str | None = None
    response_text = ""
    prompt_tokens = completion_tokens = total_tokens = 0

    create_kwargs: dict = {}
    if config.temperature is not None:
        create_kwargs["temperature"] = config.temperature
    if config.max_tokens is not None:
        # Azure OpenAI uses max_completion_tokens for all modern deployments
        # (both o-series and gpt-5.x+). The legacy max_tokens parameter is not
        # accepted by gpt-5.4 or o-series models on this API version.
        create_kwargs["max_completion_tokens"] = config.max_tokens
    if config.top_k is not None:
        create_kwargs["top_k"] = config.top_k
    if config.response_format is not None:
        create_kwargs["response_format"] = config.response_format

    # Azure OpenAI requires "json" to appear in the messages when
    # response_format={"type": "json_object"} is set, regardless of casing.
    # Append a one-line guarantee so custom prompts never trigger the 400 error.
    effective_system_prompt = system_prompt
    if (
        config.response_format is not None
        and config.response_format.get("type") == "json_object"
        and "json" not in system_prompt.lower()
        and "json" not in user_msg.lower()
    ):
        effective_system_prompt = system_prompt + "\n\nRespond with a valid JSON object only."

    try:
        response = client.chat.completions.create(
            model=config.deployment,
            messages=[
                {"role": "system", "content": effective_system_prompt},
                {"role": "user", "content": user_msg},
            ],
            **create_kwargs,
        )
        response_text = response.choices[0].message.content.strip()
        if response.usage:
            prompt_tokens = response.usage.prompt_tokens or 0
            completion_tokens = response.usage.completion_tokens or 0
            total_tokens = response.usage.total_tokens or 0
    except Exception as exc:
        error_msg = str(exc)
        raise
    finally:
        latency_ms = (time.perf_counter() - t_start) * 1000
        write_trace(LLMTrace(
            agent=agent,
            deployment=config.deployment,
            system_prompt=effective_system_prompt,
            user_msg=user_msg,
            response=response_text,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            error=error_msg,
        ))

    return response_text
