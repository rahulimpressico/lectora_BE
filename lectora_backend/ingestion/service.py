from __future__ import annotations
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import openai

from lectora_backend.ingestion.chunking.models import IngestionResult

logger = logging.getLogger(__name__)

_instance: "IngestionOrchestrator | None" = None
_executor = ThreadPoolExecutor(max_workers=2)


class IngestionOrchestrator:
    """
    Orchestrate the full document ingestion pipeline:

        Parse → Chunk → Enrich (LLM) → Embed (dedicated resource) → Index

    Two separate Azure OpenAI clients are used:
      - _llm_client       : main resource, used for metadata enrichment (chat)
      - _embeddings_client: course-embeddings resource, used exclusively for
                            generating text-embedding-3-large vectors

    All steps are individually guarded — a missing credential causes that
    step to be skipped without stopping the rest of the pipeline.
    """

    def __init__(self) -> None:
        from lectora_backend.config import settings
        self._settings = settings
        self._llm_client        = self._build_llm_client()
        self._embeddings_client = self._build_embeddings_client()
        self._search_client     = self._build_search_client()
        self._enricher          = self._build_enricher()
        self._embedder          = self._build_embedder()

    # ── Client builders ───────────────────────────────────────────────────────

    def _build_llm_client(self) -> openai.AsyncAzureOpenAI | None:
        s = self._settings
        if not s.azure_openai_endpoint or not s.azure_openai_api_key:
            logger.warning(
                "[ingestion] AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY not set "
                "— metadata enrichment will be skipped."
            )
            return None
        return openai.AsyncAzureOpenAI(
            azure_endpoint=s.azure_openai_endpoint,
            api_key=s.azure_openai_api_key,
            api_version="2024-02-01",
        )

    def _build_embeddings_client(self) -> openai.AsyncAzureOpenAI | None:
        """
        Build the dedicated client for the course-embeddings Azure OpenAI resource.

        Uses AZURE_OPENAI_EMBEDDINGS_RESOURCE_NAME + AZURE_OPENAI_EMBEDDINGS_KEY.
        Falls back to the main LLM client when the dedicated resource is not
        configured so local dev can still generate embeddings with one resource.
        """
        from lectora_backend.ingestion.embedding.embedding_service import build_embeddings_client

        s = self._settings
        client = build_embeddings_client(
            resource_name=s.azure_openai_embeddings_resource_name,
            api_key=s.azure_openai_embeddings_key,
        )
        if client is not None:
            return client

        # Graceful fallback: use main LLM resource for embeddings
        if self._llm_client is not None:
            logger.warning(
                "[ingestion] Embeddings resource not configured — falling back to "
                "main Azure OpenAI resource for embedding generation."
            )
            return self._llm_client

        return None

    def _build_search_client(self):
        s = self._settings
        if not s.azure_search_endpoint or not s.azure_search_api_key:
            logger.warning(
                "[ingestion] AZURE_SEARCH_ENDPOINT / AZURE_SEARCH_API_KEY not set "
                "— indexing will be skipped."
            )
            return None
        from lectora_backend.ingestion.storage.azure_search_client import AzureSearchIngestionClient
        return AzureSearchIngestionClient(
            endpoint=s.azure_search_endpoint,
            api_key=s.azure_search_api_key,
            index_name=s.azure_search_index_name,
        )

    def _build_enricher(self):
        if self._llm_client is None:
            return None
        s = self._settings
        deployment = (s.ingestion_llm_deployment or s.azure_openai_deployment or "").strip()
        if not deployment:
            logger.warning(
                "[ingestion] INGESTION_LLM_DEPLOYMENT (and AZURE_OPENAI_DEPLOYMENT) are not set "
                "— metadata enrichment will be skipped. Set one of these to enable it."
            )
            return None
        from lectora_backend.ingestion.enrichment.metadata_enricher import MetadataEnricher
        return MetadataEnricher(client=self._llm_client, deployment=deployment)

    def _build_embedder(self):
        if self._embeddings_client is None:
            return None
        from lectora_backend.ingestion.embedding.embedding_service import MultiLevelEmbeddingService
        return MultiLevelEmbeddingService(
            client=self._embeddings_client,
            deployment=self._settings.ingestion_embedding_deployment,
        )

    # ── Public retrieval factory ──────────────────────────────────────────────

    def build_retrieval_service(self):
        """
        Return a CourseRetrievalService wired to the same embeddings client
        and Azure AI Search index used during ingestion.

        Returns None when Azure AI Search is not configured.
        """
        s = self._settings
        if not s.azure_search_endpoint or not s.azure_search_api_key:
            return None
        if self._embeddings_client is None:
            return None
        from lectora_backend.ingestion.storage.retrieval_service import CourseRetrievalService
        return CourseRetrievalService(
            endpoint=s.azure_search_endpoint,
            api_key=s.azure_search_api_key,
            index_name=s.azure_search_index_name,
            embeddings_client=self._embeddings_client,
            embedding_deployment=s.ingestion_embedding_deployment,
        )

    # ── Ingestion pipeline ────────────────────────────────────────────────────

    async def ingest(self, file_path: str, document_id: str, filename: str) -> IngestionResult:
        """
        Run the full ingestion pipeline for a single document.

        Steps (each skipped individually if not configured):
          1. Parse   — extract DocumentTree (CPU-bound, runs in thread pool)
          2. Chunk   — build CourseChunk list with raw_text + provenance fields
          3. Enrich  — LLM metadata (summary, concepts, outcomes, difficulty)
          4. Embed   — 4 × 3072-dim vectors via dedicated embeddings resource
          5. Index   — upload to Azure AI Search (raw_text + vectors + metadata)
        """
        logger.info(
            "[ingestion] Starting: document_id=%s  file=%s", document_id, filename
        )

        # 1. Parse
        from lectora_backend.ingestion.parsers.structure_extractor import DocumentStructureExtractor
        extractor = DocumentStructureExtractor()
        tree = await asyncio.get_event_loop().run_in_executor(
            _executor, extractor.extract, file_path, document_id
        )
        logger.info(
            "[ingestion] Parsed %d sections, %d nodes",
            len(tree.sections), len(tree.flat_nodes),
        )

        # 2. Chunk
        from lectora_backend.ingestion.chunking.chunk_builder import CourseChunkBuilder
        builder = CourseChunkBuilder()
        chunks = await asyncio.get_event_loop().run_in_executor(_executor, builder.build, tree)
        logger.info("[ingestion] Built %d chunks", len(chunks))

        # 3. Enrich
        if self._enricher and chunks:
            try:
                chunks = await self._enricher.enrich_batch(chunks)
                logger.info("[ingestion] Enrichment complete")
            except Exception as exc:
                logger.warning("[ingestion] Enrichment failed, continuing: %s", exc)
        else:
            logger.info("[ingestion] Skipping enrichment (not configured or no chunks)")

        # 4. Embed
        if self._embedder and chunks:
            try:
                chunks = await self._embedder.embed_chunks(chunks)
                logger.info("[ingestion] Embedding complete")
            except Exception as exc:
                logger.warning("[ingestion] Embedding failed, continuing: %s", exc)
        else:
            logger.info("[ingestion] Skipping embedding (not configured or no chunks)")

        # 5. Index
        index_result: dict | None = None
        if self._search_client and chunks:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    _executor, self._search_client.ensure_index_exists
                )
                index_result = await asyncio.get_event_loop().run_in_executor(
                    _executor, self._search_client.upload_chunks, chunks
                )
                logger.info("[ingestion] Indexed: %s", index_result)
            except Exception as exc:
                logger.warning("[ingestion] Azure Search upload failed: %s", exc)
                raise
        else:
            logger.info("[ingestion] Skipping indexing (not configured or no chunks)")

        if index_result and index_result.get("succeeded", 0) > 0:
            outcome_status = "indexed"
        elif self._search_client and chunks:
            outcome_status = "failed"
        elif self._search_client:
            outcome_status = "parsed"
        else:
            outcome_status = "parsed"

        outcome = IngestionResult(
            document_id=document_id,
            total_sections=len(tree.sections),
            total_chunks=len(chunks),
            status=outcome_status,
        )
        logger.info(
            "[ingestion] Done: document_id=%s  sections=%d  chunks=%d  status=%s",
            outcome.document_id, outcome.total_sections,
            outcome.total_chunks, outcome.status,
        )
        return outcome

    # ── Singleton ─────────────────────────────────────────────────────────────

    @classmethod
    def get_instance(cls) -> "IngestionOrchestrator":
        global _instance
        if _instance is None:
            _instance = cls()
        return _instance
