from __future__ import annotations
import logging
import time

import openai

from lectora_backend.ingestion.chunking.models import CourseChunk
from lectora_backend.pipeline.shared_llm_config.tracer import (
    EmbeddingTrace,
    write_embedding_trace,
)

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

        # Use the first chunk's document_id as the trace reference for this batch.
        document_id: str | None = chunks[0].document_id if chunks else None
        source_file: str | None = chunks[0].source_file if chunks else None
        source_refs = [source_file] if source_file else []

        title_texts   = [c.title or "" for c in chunks]
        summary_texts = [c.metadata.summary or c.title or "" for c in chunks]
        content_texts = [c.raw_text[:_MAX_CONTENT_CHARS] for c in chunks]
        keyword_texts = [
            ", ".join(c.metadata.keywords) if c.metadata.keywords else c.title
            for c in chunks
        ]

        title_embs = await self._embed_all(
            title_texts, level="title", document_id=document_id, source_refs=source_refs
        )
        if self._endpoint_reachable is False:
            logger.warning(
                "[embedding_service] Endpoint unreachable — skipping remaining levels. "
                "Set AZURE_OPENAI_EMBEDDINGS_RESOURCE_NAME= (empty) to fall back to the "
                "main Azure OpenAI resource."
            )
            return chunks  # return without embeddings rather than burning retries

        summary_embs = await self._embed_all(
            summary_texts, level="summary", document_id=document_id, source_refs=source_refs
        )
        content_embs = await self._embed_all(
            content_texts, level="content", document_id=document_id, source_refs=source_refs
        )
        keyword_embs = await self._embed_all(
            keyword_texts, level="keywords", document_id=document_id, source_refs=source_refs
        )

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

    async def _embed_all(
        self,
        texts: list[str],
        level: str = "",
        document_id: str | None = None,
        source_refs: list[str] | None = None,
    ) -> list[list[float]]:
        """Call the embeddings API in batches, returning a flat embedding list.

        Each batch emits an EmbeddingTrace to Langfuse (latency, token usage, errors).
        On the first connection error, sets _endpoint_reachable=False and returns
        immediately so the caller can skip remaining levels without further retries.
        """
        all_embeddings: list[list[float]] = []
        batch_index = 0
        for i in range(0, len(texts), _BATCH_SIZE):
            if self._endpoint_reachable is False:
                # Endpoint already confirmed unreachable — skip remaining batches
                all_embeddings.extend([] for _ in texts[i : i + _BATCH_SIZE])
                batch_index += 1
                continue
            batch = texts[i : i + _BATCH_SIZE]
            t_start = time.perf_counter()
            error_msg: str | None = None
            total_tokens = 0
            try:
                response = await self._client.embeddings.create(
                    model=self._deployment,
                    input=batch,
                    dimensions=_DIMENSIONS,
                )
                self._endpoint_reachable = True
                if getattr(response, "usage", None):
                    total_tokens = response.usage.total_tokens or 0
                all_embeddings.extend(item.embedding for item in response.data)
            except Exception as exc:
                error_msg = str(exc)
                exc_lower = error_msg.lower()
                is_connection_error = "connection" in exc_lower or "connect" in exc_lower
                if is_connection_error and self._endpoint_reachable is None:
                    self._endpoint_reachable = False
                logger.warning(
                    "[embedding_service] Batch %d/%s failed: %s", i, level, exc
                )
                all_embeddings.extend([] for _ in batch)
            finally:
                latency_ms = (time.perf_counter() - t_start) * 1000
                try:
                    write_embedding_trace(EmbeddingTrace(
                        agent="INGEST_EMBED",
                        deployment=self._deployment,
                        level=level,
                        batch_index=batch_index,
                        batch_size=len(batch),
                        dimensions=_DIMENSIONS,
                        latency_ms=latency_ms,
                        total_tokens=total_tokens,
                        error=error_msg,
                        document_id=document_id,
                        source_refs=source_refs or [],
                        # Ingestion runs in its own async tasks; supply explicit IDs
                        # rather than relying on context vars (which may not be set).
                        run_id=f"ingest:{document_id}" if document_id else "",
                        doc_name=document_id or "ingestion",
                    ))
                except Exception:
                    pass  # tracing must never break the ingestion pipeline

            if self._endpoint_reachable is False:
                # Abort this level immediately — no point retrying other batches
                remaining = texts[i + _BATCH_SIZE :]
                all_embeddings.extend([] for _ in remaining)
                break
            batch_index += 1
        return all_embeddings
