from __future__ import annotations
import logging

import openai

from lectora_backend.ingestion.chunking.models import CourseChunk

logger = logging.getLogger(__name__)

_DIMENSIONS = 3072
_BATCH_SIZE = 16
_MAX_CONTENT_CHARS = 8000


def build_embeddings_client(resource_name: str, api_key: str):
    """
    Build a dedicated AsyncAzureOpenAI client for the embeddings resource.

    Endpoint is derived from the resource name:
        https://{resource_name}.openai.azure.com/

    Returns None if either credential is missing, allowing the caller to
    skip embedding gracefully.
    """
    import openai

    if not resource_name or not api_key:
        logger.warning(
            "[embedding_service] AZURE_OPENAI_EMBEDDINGS_RESOURCE_NAME or "
            "AZURE_OPENAI_EMBEDDINGS_KEY is not set — embedding will be skipped."
        )
        return None

    endpoint = f"https://{resource_name.strip()}.openai.azure.com/"
    logger.info("[embedding_service] Embeddings client → %s", endpoint)
    return openai.AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version="2024-02-01",
        max_retries=1,  # fail fast on connection errors; ingestion continues without embeddings
    )


class MultiLevelEmbeddingService:
    """
    Embed CourseChunk objects at 4 levels using a dedicated embeddings resource.

    Levels:
        embedding_title    — section title (used for outline / navigation retrieval)
        embedding_summary  — LLM-generated summary (used for LO / objective retrieval)
        embedding_content  — full raw_text (used for topic and assessment retrieval)
        embedding_keywords — joined keywords (used for quiz / knowledge-gap retrieval)
    """

    def __init__(self, client, deployment: str) -> None:
        self._client = client
        self._deployment = deployment
        self._endpoint_reachable: bool | None = None  # None = untested

    async def embed_chunks(self, chunks: list[CourseChunk]) -> list[CourseChunk]:
        """Generate 4 embedding vectors per chunk in batches."""
        if not chunks:
            return chunks

        title_texts   = [c.title or "" for c in chunks]
        summary_texts = [c.metadata.summary or c.title or "" for c in chunks]
        content_texts = [c.raw_text[:_MAX_CONTENT_CHARS] for c in chunks]
        keyword_texts = [
            ", ".join(c.metadata.keywords) if c.metadata.keywords else c.title
            for c in chunks
        ]

        title_embs   = await self._embed_all(title_texts,   level="title")
        if self._endpoint_reachable is False:
            logger.warning(
                "[embedding_service] Endpoint unreachable — skipping remaining levels. "
                "Set AZURE_OPENAI_EMBEDDINGS_RESOURCE_NAME= (empty) to fall back to the "
                "main Azure OpenAI resource."
            )
            return chunks  # return without embeddings rather than burning retries

        summary_embs = await self._embed_all(summary_texts, level="summary")
        content_embs = await self._embed_all(content_texts, level="content")
        keyword_embs = await self._embed_all(keyword_texts, level="keywords")

        enriched: list[CourseChunk] = []
        for i, chunk in enumerate(chunks):
            enriched.append(chunk.model_copy(update={
                "embedding_title":    title_embs[i]   if i < len(title_embs)   else None,
                "embedding_summary":  summary_embs[i] if i < len(summary_embs) else None,
                "embedding_content":  content_embs[i] if i < len(content_embs) else None,
                "embedding_keywords": keyword_embs[i] if i < len(keyword_embs) else None,
            }))

        logger.info("[embedding_service] Embedded %d chunks × 4 levels", len(enriched))
        return enriched

    async def _embed_all(self, texts: list[str], level: str = "") -> list[list[float]]:
        """Call the embeddings API in batches, returning a flat embedding list.

        On the first connection error, sets _endpoint_reachable=False and returns
        immediately so the caller can skip remaining levels without further retries.
        """
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            if self._endpoint_reachable is False:
                # Endpoint already confirmed unreachable — skip remaining batches
                all_embeddings.extend([] for _ in texts[i : i + _BATCH_SIZE])
                continue
            batch = texts[i : i + _BATCH_SIZE]
            try:
                response = await self._client.embeddings.create(
                    model=self._deployment,
                    input=batch,
                    dimensions=_DIMENSIONS,
                )
                self._endpoint_reachable = True
                all_embeddings.extend(item.embedding for item in response.data)
            except Exception as exc:
                exc_str = str(exc).lower()
                is_connection_error = "connection" in exc_str or "connect" in exc_str
                if is_connection_error and self._endpoint_reachable is None:
                    self._endpoint_reachable = False
                logger.warning(
                    "[embedding_service] Batch %d/%s failed: %s", i, level, exc
                )
                all_embeddings.extend([] for _ in batch)
                if self._endpoint_reachable is False:
                    # Abort this level immediately — no point retrying other batches
                    remaining = texts[i + _BATCH_SIZE :]
                    all_embeddings.extend([] for _ in remaining)
                    break
        return all_embeddings
