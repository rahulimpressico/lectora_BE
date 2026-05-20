"""Langfuse client for LLM trace logging."""
import logging
from lectora_backend.config import settings

logger = logging.getLogger(__name__)

try:
    from langfuse import Langfuse
    _langfuse = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
except Exception:
    _langfuse = None
    logger.warning("Langfuse not configured – tracing disabled")


def get_langfuse():
    return _langfuse
