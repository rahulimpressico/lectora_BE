"""
Vector retriever for Section Mapper.

Queries the Azure AI Search index (populated by the ingestion pipeline) to find
the most semantically relevant source chunks for each lesson and subtopic.

This module is the sole Retrieval Layer for the Section Mapper.  It owns:
  - Query construction          (build_query, _build_subtopic_query)
  - Lesson-level vector search  (VectorRetriever.retrieve_for_lesson)
  - Subtopic-level retrieval    (VectorRetriever.distribute_to_subtopics)
  - Result parsing + threshold  (_parse_search_results)
  - Document-order sorting      (_sort_by_document_order)
  - Text aggregation            (merge_to_raw_text)

Retrieval architecture
──────────────────────
1. Lesson-level retrieval (one call per lesson):
     build_query(title + subtopics + objectives)
       → Azure AI Search hybrid (BM25 + vector on content + summary)
       → top-20 chunks, dynamic threshold filter
       → candidate pool used for logging and fallback

2. Subtopic-level retrieval (one call per content subtopic):
     _build_subtopic_query(subtopic_title, lesson_title)
       → Azure AI Search hybrid + semantic ranker
       → top-5 chunks per subtopic, dynamic threshold filter
       → sorted by document order, attached as matched_chunks

The semantic ranker (when available) is a cross-encoder ML model that applies
language-level understanding on top of BM25+vector fusion.  It correctly maps
"Understanding Health Plan Obligations" → "ACA Employer Shared Responsibility
Provisions" without any keyword overlap.

Dynamic threshold
──────────────────
  threshold = max(MIN_ABSOLUTE_THRESHOLD, top_score × DYNAMIC_THRESHOLD_RATIO)

  Tightens the cut-off when strong results are returned; relaxes it when the
  query is ambiguous.  Score is derived from @search.rerankerScore / 4.0 when
  the semantic ranker is active, otherwise from @search.score.

Keyword fallback
─────────────────
  When the per-subtopic Azure AI Search call fails entirely (network error,
  service unavailable), a keyword-overlap ranking over the lesson-level candidate
  pool is used.  This is an emergency path only — it does not affect accuracy
  during normal operation.

Public surface
───────────────
    get_retriever()                             → VectorRetriever | None
    build_query(title, ...)                     → str
    merge_to_raw_text(chunks)                   → str
    VectorChunk                                 dataclass
    VectorRetriever.retrieve_for_lesson(…)
    VectorRetriever.distribute_to_subtopics(…)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# Lesson-level retrieval — broad candidate pool for fallback + logging.
_LESSON_TOP_K = 20

# Per-subtopic retrieval — enough to cover a subtopic after threshold filtering.
_SUBTOPIC_TOP_K = 5

# Dynamic threshold for score filtering.
# threshold = max(MIN_ABSOLUTE, top_score × RATIO)
# For reranker scores (0–1 normalised): top=0.90 → threshold≈0.63
# For search scores (0–1):             top=0.60 → threshold=0.42 (floor at 0.20)
_MIN_ABSOLUTE_THRESHOLD = 0.20
_DYNAMIC_THRESHOLD_RATIO = 0.70

# Keyword fallback helpers.
_MIN_WORD_LEN = 3
_STOP_WORDS = frozenset({
    "and", "the", "for", "with", "that", "this", "are", "from",
    "have", "has", "its", "not", "but", "was", "will", "can",
    "may", "all", "any", "how", "their",
})


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class VectorChunk:
    """A single source chunk retrieved from Azure AI Search."""
    raw_text: str
    similarity_score: float
    source_metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "raw_text":         self.raw_text,
            "similarity_score": self.similarity_score,
            "source_metadata":  self.source_metadata,
        }


# ── Query builders ──────────────────────────────────────────────────────────────

def build_query(
    title: str,
    subtopic_titles: list[str] | None = None,
    objectives: list[str] | None = None,
) -> str:
    """
    Build a natural-language search query for lesson-level retrieval.

    Combines the lesson title, subtopic headings, and learning objectives into
    a single text for hybrid (BM25 + vector) Azure AI Search.
    """
    parts: list[str] = []
    if title:
        parts.append(title.strip())
    if subtopic_titles:
        parts.extend(t.strip() for t in subtopic_titles[:4] if t and t.strip())
    if objectives:
        parts.extend(o.strip() for o in objectives[:3] if o and o.strip())
    return ". ".join(filter(None, parts))


def _build_subtopic_query(subtopic_title: str, lesson_title: str) -> str:
    """
    Build a targeted query for per-subtopic retrieval.

    The subtopic title is the primary signal; the lesson title is appended as
    context to disambiguate terms that mean different things in different modules
    (e.g. "liability" in a property course vs. a health insurance course).
    """
    parts = [subtopic_title.strip()]
    if lesson_title and lesson_title.strip() != subtopic_title.strip():
        parts.append(lesson_title.strip())
    return ". ".join(filter(None, parts))


# ── Result parsing ──────────────────────────────────────────────────────────────

def _parse_search_results(
    raw: list[dict],
    threshold_ratio: float = _DYNAMIC_THRESHOLD_RATIO,
) -> list[VectorChunk]:
    """
    Convert raw Azure AI Search result dicts to VectorChunk objects.

    Score selection
    ────────────────
    When the semantic ranker is active, each result carries
    ``@search.rerankerScore`` (0–4).  This is normalised to [0, 1] by dividing
    by 4.0 and used as ``similarity_score`` in preference to ``@search.score``,
    because the reranker score is a language-level relevance signal (not just an
    RRF fusion of BM25 + vector distances).

    When the reranker is absent, ``@search.score`` (the RRF-fused score) is used.

    Dynamic threshold
    ──────────────────
    threshold = max(MIN_ABSOLUTE_THRESHOLD, top_score × threshold_ratio)

    Calibrates the cut-off to the actual quality of each result set rather than
    using a fixed value that may be too strict or too loose across queries.

    ``source_metadata["reranker_score"]`` preserves the raw reranker value for
    logging and diagnostics.
    """
    if not raw:
        return []

    chunks: list[VectorChunk] = []
    for r in raw:
        raw_text = (r.get("raw_text") or "").strip()
        if not raw_text:
            continue

        reranker_raw = r.get("@search.rerankerScore")
        search_score = float(r.get("@search.score") or 0.0)
        # Normalise reranker score (0–4) to [0, 1]; fall back to search score.
        score = (float(reranker_raw) / 4.0) if reranker_raw is not None else search_score

        chunks.append(VectorChunk(
            raw_text=raw_text,
            similarity_score=round(score, 4),
            source_metadata={
                "chunk_id":       r.get("chunk_id", ""),
                "source_file":    r.get("source_file", ""),
                "page_num":       r.get("page_num"),
                "title":          r.get("title", ""),
                "section_id":     r.get("section_id", ""),
                "summary":        r.get("summary", ""),
                "reranker_score": reranker_raw,
            },
        ))

    if not chunks:
        return chunks

    top_score = max(c.similarity_score for c in chunks)
    threshold = max(_MIN_ABSOLUTE_THRESHOLD, top_score * threshold_ratio)
    filtered = [c for c in chunks if c.similarity_score >= threshold]

    logger.debug(
        "[vector_retriever] parse: %d/%d passed threshold=%.3f "
        "(top=%.3f, has_reranker=%s)",
        len(filtered), len(chunks), threshold, top_score,
        chunks[0].source_metadata.get("reranker_score") is not None if chunks else False,
    )
    return filtered


# ── Document-order sort ────────────────────────────────────────────────────────

def _sort_by_document_order(chunks: list[VectorChunk]) -> list[VectorChunk]:
    """
    Re-sort chunks by their natural document order (page_num → sequence in chunk_id).

    Preserves reading order when multiple chunks are merged into a content block,
    which produces more coherent source text for A2's generation prompt.
    """
    def _order_key(c: VectorChunk) -> tuple:
        meta = c.source_metadata
        page = meta.get("page_num") or 0
        chunk_id = meta.get("chunk_id", "")
        try:
            seq = int(chunk_id.rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            seq = 0
        return (page, seq)

    return sorted(chunks, key=_order_key)


# ── Keyword fallback ────────────────────────────────────────────────────────────

def _keyword_tokens(text: str) -> frozenset[str]:
    """Extract lowercase meaningful tokens for emergency keyword fallback."""
    words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    return frozenset(w for w in words if w not in _STOP_WORDS and len(w) >= _MIN_WORD_LEN)


def _keyword_fallback(
    subtopic_title: str,
    lesson_chunks: list[VectorChunk],
    top: int,
) -> list[VectorChunk]:
    """
    Emergency fallback: rank lesson-level chunks by keyword overlap with subtopic title.

    Used only when the per-subtopic Azure AI Search call fails entirely
    (network error, service unavailable).  This path does NOT affect accuracy
    during normal operation — it prevents the subtopic from having empty
    matched_chunks due to a transient error.
    """
    if not lesson_chunks:
        return []

    sub_tokens = _keyword_tokens(subtopic_title)
    if not sub_tokens:
        return lesson_chunks[:top]

    scored = [
        (chunk, len(sub_tokens & _keyword_tokens(chunk.raw_text[:500])))
        for chunk in lesson_chunks
    ]
    scored.sort(key=lambda t: t[1], reverse=True)
    return [c for c, _ in scored[:top]]


# ── Retriever ──────────────────────────────────────────────────────────────────

class VectorRetriever:
    """
    Retrieves and distributes source chunks from Azure AI Search for the Section Mapper.

    Design
    ──────
    - One Azure AI Search hybrid call per lesson gives a broad candidate pool
      used for monitoring and emergency fallback.
    - One targeted Azure AI Search call per content subtopic uses the semantic
      ranker (when available) for concept-level precision.  This is the primary
      path for matched_chunks assignment.
    - Semantic ranking resolves paraphrase / terminology-mismatch problems that
      keyword overlap and cosine similarity both fail on: a query about
      "Health Plan Obligations" surfaces "ACA Employer Shared Responsibility"
      chunks because the cross-encoder understands semantic equivalence.
    - When the semantic call fails, the lesson-level candidate pool is used as
      a keyword-ranked fallback so no subtopic is left with empty matched_chunks
      due to a transient error.
    """

    def __init__(self, retrieval_service) -> None:
        self._service = retrieval_service

    # ── Primary API ──────────────────────────────────────────────────────────

    def retrieve_for_lesson(
        self,
        lesson_title: str,
        subtopic_titles: list[str] | None = None,
        objectives: list[str] | None = None,
        document_id: str | None = None,
        top: int = _LESSON_TOP_K,
    ) -> list[VectorChunk]:
        """
        Fetch a broad candidate pool for an entire lesson.

        This is a lesson-level hybrid search (BM25 + vector, no semantic ranker).
        The result serves two purposes:
          1. Monitoring — total chunks available, score distribution per lesson.
          2. Emergency fallback — keyword-ranked source for subtopics whose
             per-subtopic Azure AI Search call fails.

        For the primary subtopic content assignment, call distribute_to_subtopics()
        which issues individual semantic-ranked queries per subtopic.

        Returns VectorChunks above the adaptive threshold, sorted by score desc.
        """
        query = build_query(lesson_title, subtopic_titles, objectives)
        logger.info(
            "[vector_retriever] Lesson=%r  query=%r  document_id=%s  top=%d",
            lesson_title[:50], query[:120], document_id, top,
        )

        try:
            raw = self._service.retrieve_topic(
                topic=query,
                document_id=document_id,
                top=top,
            )
        except Exception as exc:
            logger.warning(
                "[vector_retriever] Lesson search failed for %r: %s",
                lesson_title[:50], exc,
            )
            return []

        chunks = _parse_search_results(raw)
        chunks.sort(key=lambda c: c.similarity_score, reverse=True)

        logger.info(
            "[vector_retriever] Lesson=%r → %d/%d chunks above threshold",
            lesson_title[:50], len(chunks), len(raw),
        )
        if chunks:
            logger.debug(
                "[vector_retriever] Top 3 lesson scores: %s  top_title=%r",
                [f"{c.similarity_score:.3f}" for c in chunks[:3]],
                chunks[0].source_metadata.get("title", "")[:50],
            )

        return chunks

    def distribute_to_subtopics(
        self,
        lesson_chunks: list[VectorChunk],
        subtopics: list[dict],
        lesson_title: str = "",
        document_id: str | None = None,
        top_per_subtopic: int = _SUBTOPIC_TOP_K,
    ) -> list[dict]:
        """
        Assign relevant chunks to each subtopic via targeted Azure AI Search retrieval.

        Primary path: per-subtopic semantic search
        ────────────────────────────────────────────
        For each content subtopic, a targeted query is issued:
            query = "{subtopic_title}. {lesson_title}"

        The lesson title provides disambiguation context.  The semantic ranker
        (when available) re-scores the top-50 BM25+vector candidates using a
        cross-encoder model, correctly mapping concepts that share no keywords:

            Query: "Understanding Health Plan Obligations"
            Result: "ACA Employer Shared Responsibility Provisions" ✓

        Fallback path: keyword overlap over lesson pool
        ─────────────────────────────────────────────────
        When the per-subtopic search call fails (network, service error), the
        lesson_chunks pool is ranked by keyword overlap with the subtopic title.
        This is an emergency path only; it does not affect normal-operation accuracy.

        Score handling
        ───────────────
        @search.rerankerScore (0–4, normalised to 0–1) is preferred over
        @search.score when the semantic ranker is active.  The dynamic threshold
        filters results: threshold = max(0.20, top_score × 0.70).

        Modifies subtopics in-place by adding "matched_chunks" to each entry.
        KC-only subtopics are skipped (they use template content, not source text).
        Returns the augmented subtopic list.

        Args:
            lesson_chunks:     Lesson-level candidate pool (monitoring + fallback).
            subtopics:         Mutable list of subtopic dicts from mapper.py.
            lesson_title:      TO lesson heading — appended to subtopic query for context.
            document_id:       Optional — restrict search to a specific ingested document.
            top_per_subtopic:  Max chunks per subtopic (default 5).
        """
        if not subtopics:
            return subtopics

        for sub in subtopics:
            if sub.get("is_knowledge_check") or sub.get("has_knowledge_check"):
                continue

            sub_title = sub.get("title", "")
            query = _build_subtopic_query(sub_title, lesson_title)

            logger.debug(
                "[vector_retriever] Subtopic=%r  query=%r  document_id=%s",
                sub_title[:50], query[:120], document_id,
            )

            raw: list[dict] = []
            try:
                raw = self._service.retrieve_for_subtopic(
                    subtopic_query=query,
                    document_id=document_id,
                    top=top_per_subtopic,
                )
            except Exception as exc:
                logger.warning(
                    "[vector_retriever] Subtopic search failed for %r: %s — "
                    "using lesson-level keyword fallback.",
                    sub_title[:50], exc,
                )

            if raw:
                chunks = _parse_search_results(raw)
                ordered = _sort_by_document_order(chunks)
                sub["matched_chunks"] = [c.as_dict() for c in ordered]

                has_reranker = any(
                    c.source_metadata.get("reranker_score") is not None
                    for c in ordered[:1]
                )
                logger.debug(
                    "[vector_retriever] Subtopic=%r → %d chunks  "
                    "scores=%s  semantic_ranked=%s",
                    sub_title[:50],
                    len(ordered),
                    [f"{c.similarity_score:.3f}" for c in ordered],
                    has_reranker,
                )
            else:
                # Emergency fallback: keyword overlap over lesson-level pool.
                fallback = _keyword_fallback(sub_title, lesson_chunks, top_per_subtopic)
                ordered = _sort_by_document_order(fallback)
                sub["matched_chunks"] = [c.as_dict() for c in ordered]
                logger.debug(
                    "[vector_retriever] Subtopic=%r — keyword fallback (%d chunks)",
                    sub_title[:50], len(ordered),
                )

        return subtopics


# ── Text aggregation ────────────────────────────────────────────────────────────

def merge_to_raw_text(chunks: list[VectorChunk], separator: str = "\n\n") -> str:
    """
    Merge a list of VectorChunks into a single coherent text block.

    Chunks should already be sorted by document order (as returned by
    distribute_to_subtopics).  Deduplicates identical paragraphs that
    occasionally appear when chunk boundaries overlap.

    Args:
        chunks:    Ordered list of VectorChunk objects.
        separator: String placed between chunks (default: blank line).

    Returns:
        Single string ready to pass to A2 as source_text.
    """
    seen: set[str] = set()
    parts: list[str] = []
    for chunk in chunks:
        text = chunk.raw_text.strip()
        if text and text not in seen:
            parts.append(text)
            seen.add(text)
    return separator.join(parts)


# ── Factory ─────────────────────────────────────────────────────────────────────

_retriever_cache: VectorRetriever | None = None
_retriever_attempted: bool = False


def get_retriever() -> VectorRetriever | None:
    """
    Return a configured VectorRetriever, or None if Azure AI Search is not set up.

    Caches the result after the first call so subsequent lessons share a single
    retriever instance (and the semantic availability probe result is reused).
    Returns None silently when search is unavailable — the caller (mapper.py)
    falls back to BM25 source_chunks from shared_state.
    """
    global _retriever_cache, _retriever_attempted
    if _retriever_attempted:
        return _retriever_cache

    _retriever_attempted = True
    try:
        from lectora_backend.ingestion.service import IngestionOrchestrator
        service = IngestionOrchestrator.get_instance().build_retrieval_service()
        if service is None:
            logger.info(
                "[vector_retriever] Azure AI Search not configured — "
                "subtopics will have no matched_chunks; A2 uses BM25 fallback."
            )
            return None
        _retriever_cache = VectorRetriever(service)
        logger.info("[vector_retriever] Retriever initialised successfully.")
    except Exception as exc:
        logger.warning(
            "[vector_retriever] Could not initialise retriever: %s — "
            "section mapper will run without vector retrieval.", exc,
        )
        _retriever_cache = None

    return _retriever_cache
