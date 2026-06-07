"""Azure OpenAI wrapper with token usage capture."""
import logging
from openai import AsyncAzureOpenAI
from lectora_backend.config import settings

logger = logging.getLogger(__name__)


class AzureOpenAIClient:
    def __init__(self) -> None:
        self._client = AsyncAzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version="2025-01-01-preview",
        )
        self._deployment = settings.azure_openai_deployment

    async def chat(self, messages: list[dict], **kwargs) -> tuple[str, dict]:
        response = await self._client.chat.completions.create(
            model=self._deployment,
            messages=messages,
            **kwargs,
        )
        content = response.choices[0].message.content or ""
        usage = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        }
        logger.debug("Token usage: %s", usage)
        return content, usage
