from __future__ import annotations
import logging

import httpx
import openai

logger = logging.getLogger(__name__)

_API_VERSION = "2024-07-01"
_EMBEDDING_DIMS = 3072
_DEFAULT_TOP = 5

# Name of the semantic configuration defined in index_schema.py.
# Azure AI Search uses this name to look up the prioritized-fields definition
# when queryType="semantic" is requested.
_SEMANTIC_CONFIG_NAME = "default"

# Fields returned with every search result — raw_text is always included so
# callers receive the verbatim chunk text alongside the matched score.
_DEFAULT_SELECT = (
    "chunk_id,document_id,section_id,source_file,page_num,"
    "title,parent_title,raw_text,summary,"
    "keywords,difficulty,token_count,estimated_read_min"
)


class CourseRetrievalService:
    """
    Retrieve course content from Azure AI Search using hybrid search
    (BM25 keyword + vector + optional semantic reranker).

    Semantic ranking
    ────────────────
    When ``use_semantic=True`` is passed to ``_search()``, the request uses
    ``queryType: "semantic"`` which activates Azure AI Search's cross-encoder
    reranker.  The reranker re-scores the top-50 candidates from BM25+vector
    fusion using an ML model trained on semantic equivalence — meaning a query
    "Understanding Health Plan Obligations" will correctly surface chunks about
    "ACA Employer Shared Responsibility Provisions" even with zero keyword overlap.

    Availability probing
    ─────────────────────
    Semantic ranking requires Standard tier (S1+) and semantic search enabled on
    the index.  The first semantic request probes availability: if Azure AI Search
    returns 400 with a semantic-related message, ``_semantic_available`` is set to
    False and all subsequent calls transparently fall back to hybrid (BM25+vector).
    The probe result is cached for the lifetime of this instance.

    Every result always includes raw_text so the caller does not need a separate
    blob lookup to reconstruct the original content.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        index_name: str,
        embeddings_client: openai.AsyncAzureOpenAI,
        embedding_deployment: str,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._index_name = index_name
        self._embeddings_client = embeddings_client
        self._embedding_deployment = embedding_deployment
        self._search_headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }
        # None = not yet probed; True = confirmed available; False = unavailable.
        self._semantic_available: bool | None = None

    # ── Query embedding ───────────────────────────────────────────────────────

    def embed_query(self, text: str) -> list[float]:
        """
        Synchronously embed a single query string.

        Delegates to embed_batch() for a unified code path.
        Returns an empty list on failure so callers fall back to BM25-only search.
        """
        result = self.embed_batch([text])
        return result[0] if result else []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Synchronously embed multiple texts in a single API call.

        One HTTP round-trip regardless of batch size.  The Azure OpenAI billing
        unit (tokens) is identical to calling embed_query() N times.

        Returns a list of embedding vectors in the same order as ``texts``.
        On failure returns a list of empty lists (one per input).
        """
        if not texts:
            return []
        import asyncio
        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is not None:
                # Called from inside a running event loop (e.g. an async route).
                # Dispatch to a fresh thread so asyncio.run() can own its own loop.
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(
                        asyncio.run, self._async_embed_batch(texts)
                    ).result(timeout=60)
            else:
                # Called from a sync context or a worker thread with no event loop
                # (e.g. ThreadPoolExecutor-*).  asyncio.run() creates its own loop.
                return asyncio.run(self._async_embed_batch(texts))
        except Exception as exc:
            logger.warning("[retrieval] embed_batch failed (%d texts): %s", len(texts), exc)
            return [[] for _ in texts]

    async def _async_embed_batch(self, texts: list[str]) -> list[list[float]]:
        response = await self._embeddings_client.embeddings.create(
            model=self._embedding_deployment,
            input=texts,
            dimensions=_EMBEDDING_DIMS,
        )
        # Sort by index to guarantee input order is preserved (API contract).
        sorted_data = sorted(response.data, key=lambda e: e.index)
        return [item.embedding for item in sorted_data]

    # ── Core search ───────────────────────────────────────────────────────────

    def _search(
        self,
        query: str,
        vector_fields: list[str] | None = None,
        filters: str | None = None,
        top: int = _DEFAULT_TOP,
        select: str = _DEFAULT_SELECT,
        use_semantic: bool = False,
    ) -> list[dict]:
        """
        Execute a hybrid search request and return raw result dicts.

        Hybrid search
        ──────────────
        Every call issues BM25 full-text search and vector similarity search in
        parallel.  Azure AI Search fuses the two result lists via Reciprocal Rank
        Fusion (RRF) and returns a single ranked list sorted by ``@search.score``.

        Semantic ranking (optional)
        ────────────────────────────
        When ``use_semantic=True`` and the index supports it, the request sets
        ``queryType: "semantic"`` which activates a cross-encoder reranker over the
        top-50 BM25+vector candidates.  The reranker score is returned in
        ``@search.rerankerScore`` (0-4 scale, higher = more semantically relevant).

        Availability is probed on the first semantic call and cached.  If the index
        is not configured for semantic search, this method transparently retries
        without semantic and logs a one-time warning.

        Each result dict includes raw_text so the caller always has both the
        relevance signal and the source content without a separate blob lookup.
        """
        url = (
            f"{self._endpoint}/indexes/{self._index_name}/docs/search"
            f"?api-version={_API_VERSION}"
        )
        embedding = self.embed_query(query)
        target_fields = vector_fields or ["embedding_content", "embedding_summary"]

        vector_queries = [
            {"kind": "vector", "vector": embedding, "fields": field, "k": top}
            for field in target_fields
            if embedding
        ]

        def _build_payload(semantic: bool) -> dict:
            p: dict = {
                "search":        query,
                "queryType":     "semantic" if semantic else "simple",
                "vectorQueries": vector_queries,
                "top":           top,
                "select":        select,
            }
            if semantic:
                p["semanticConfiguration"] = _SEMANTIC_CONFIG_NAME
            if filters:
                p["filter"] = filters
            return p

        want_semantic = use_semantic and (self._semantic_available is not False)
        payload = _build_payload(want_semantic)

        try:
            resp = httpx.post(
                url, json=payload, headers=self._search_headers, timeout=30
            )

            # Detect semantic not configured: Azure AI Search returns 400 with a
            # descriptive message when semanticConfiguration or the feature itself
            # is absent.  Retry without semantic and cache the result.
            if want_semantic and resp.status_code in (400, 422):
                body_lower = resp.text.lower()
                if any(kw in body_lower for kw in ("semantic", "semanticconfiguration")):
                    logger.info(
                        "[retrieval] Semantic ranker not available on this index "
                        "(HTTP %d) — disabling for all subsequent calls.",
                        resp.status_code,
                    )
                    self._semantic_available = False
                    payload = _build_payload(False)
                    resp = httpx.post(
                        url, json=payload, headers=self._search_headers, timeout=30
                    )

            resp.raise_for_status()

            if want_semantic and self._semantic_available is None:
                self._semantic_available = True
                logger.info("[retrieval] Azure AI Search semantic ranker confirmed active.")

            results = resp.json().get("value", [])
            has_reranker = any(r.get("@search.rerankerScore") is not None for r in results[:1])
            logger.info(
                "[retrieval] Query='%s' → %d results "
                "(semantic=%s, reranker_active=%s, fields=%s)",
                query[:60], len(results),
                self._semantic_available if use_semantic else False,
                has_reranker,
                target_fields,
            )
            return results

        except Exception as exc:
            logger.warning("[retrieval] Search failed for query='%s': %s", query[:60], exc)
            return []

    # ── Retrieval strategies ──────────────────────────────────────────────────

    def retrieve_topic(
        self,
        topic: str,
        document_id: str | None = None,
        top: int = _DEFAULT_TOP,
    ) -> list[dict]:
        """
        Retrieve chunks most relevant to a topic (lesson-level).

        Uses hybrid BM25 + vector search without semantic ranking — semantic
        ranking is reserved for subtopic-level queries where per-concept
        precision is more important than broad lesson coverage.

        Primary use: lesson body monitoring, fallback pool for subtopic distribution.
        """
        filters = f"document_id eq '{document_id}'" if document_id else None
        return self._search(
            topic,
            vector_fields=["embedding_content", "embedding_summary"],
            filters=filters,
            top=top,
        )

    def retrieve_for_subtopic(
        self,
        subtopic_query: str,
        document_id: str | None = None,
        top: int = _DEFAULT_TOP,
    ) -> list[dict]:
        """
        Retrieve chunks for a single subtopic using semantic ranking when available.

        This is the primary retrieval method for subtopic content assignment.
        Semantic ranking resolves paraphrase and terminology-mismatch problems —
        for example, a query "Understanding Health Plan Obligations" correctly
        surfaces chunks about "ACA Employer Shared Responsibility Provisions"
        even though the two phrases share no keywords.

        The ``@search.rerankerScore`` (0-4) in each result indicates semantic
        relevance.  Callers should prefer it over ``@search.score`` when present.

        Falls back to hybrid BM25+vector automatically when semantic is unavailable.
        """
        filters = f"document_id eq '{document_id}'" if document_id else None
        return self._search(
            subtopic_query,
            vector_fields=["embedding_content", "embedding_summary"],
            filters=filters,
            top=top,
            use_semantic=True,
        )

    def retrieve_for_outline(
        self,
        outline_topic: str,
        document_id: str | None = None,
        top: int = _DEFAULT_TOP,
    ) -> list[dict]:
        """
        Retrieve high-level section chunks for course outline / module scaffolding.

        Uses title and summary embeddings to surface structural sections rather
        than deep content.  Returns raw_text so the caller can inspect section
        bodies when building module descriptions.
        """
        filters = f"document_id eq '{document_id}'" if document_id else None
        return self._search(
            outline_topic,
            vector_fields=["embedding_title", "embedding_summary"],
            filters=filters,
            top=top,
        )

    def retrieve_for_objectives(
        self,
        objective: str,
        document_id: str | None = None,
        top: int = _DEFAULT_TOP,
    ) -> list[dict]:
        """
        Retrieve chunks aligned to a learning objective.

        Uses content and keyword embeddings.  Returns raw_text alongside
        learning_concepts and learning_outcomes for LO generation.
        Primary use: KC planner, learning objective generation.
        """
        filters = f"document_id eq '{document_id}'" if document_id else None
        return self._search(
            objective,
            vector_fields=["embedding_content", "embedding_keywords"],
            filters=filters,
            top=top,
            select=_DEFAULT_SELECT + ",learning_concepts,learning_outcomes,prerequisites",
        )

    def retrieve_for_assessment(
        self,
        topic: str,
        document_id: str | None = None,
        difficulty: str | None = None,
        top: int = _DEFAULT_TOP,
    ) -> list[dict]:
        """
        Retrieve chunks suitable for quiz / assessment question generation.

        Filters by difficulty when provided.  Returns raw_text so the LLM
        can generate questions directly from the source text.
        Primary use: knowledge check generation, question banks.
        """
        filter_parts: list[str] = []
        if document_id:
            filter_parts.append(f"document_id eq '{document_id}'")
        if difficulty:
            filter_parts.append(f"difficulty eq '{difficulty}'")
        filters = " and ".join(filter_parts) or None

        return self._search(
            topic,
            vector_fields=["embedding_content", "embedding_keywords"],
            filters=filters,
            top=top,
        )
