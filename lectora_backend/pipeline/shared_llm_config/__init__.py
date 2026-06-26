"""Shared Azure OpenAI client and JSONL tracer used by all pipeline agents."""

from .llm import LLMConfig, chat, get_client
from .tracer import (
    flush_langfuse,
    set_doc_name,
    set_run_id,
    set_run_context,
    set_source_refs,
    shutdown_langfuse,
)

__all__ = [
    "LLMConfig",
    "chat",
    "get_client",
    "set_doc_name",
    "set_run_id",
    "set_run_context",
    "set_source_refs",
    "flush_langfuse",
    "shutdown_langfuse",
]
