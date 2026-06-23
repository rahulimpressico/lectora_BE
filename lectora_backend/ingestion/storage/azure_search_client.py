from __future__ import annotations
import logging
from datetime import datetime, timezone

import httpx

from lectora_backend.ingestion.chunking.models import CourseChunk
from lectora_backend.ingestion.storage.index_schema import get_index_definition

logger = logging.getLogger(__name__)

# Vector-heavy documents exceed Azure's 16 MB batch limit well before 100 docs.
_BATCH_SIZE = 15
_API_VERSION = "2024-07-01"
_REQUEST_TIMEOUT = 300


class AzureSearchIngestionClient:
    """
    Upload CourseChunk documents to Azure AI Search via the REST API.

    Each document stored in the index contains:
        - chunk_id / document_id / section_id   — identity
        - title / parent_title                  — navigation
        - raw_text                              — full verbatim chunk text (retrievable)
        - summary / keywords / skills / …       — LLM-enriched metadata
        - source_file / page_num                — source provenance
        - embedding_title/summary/content/keywords — 4 × 3072-dim vectors
    """

    def __init__(self, endpoint: str, api_key: str, index_name: str) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._index_name = index_name
        self._headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }

    # ── Index management ──────────────────────────────────────────────────────

    def ensure_index_exists(self) -> None:
        """Create the Azure AI Search index if it does not already exist."""
        index_url = f"{self._endpoint}/indexes/{self._index_name}?api-version={_API_VERSION}"
        try:
            resp = httpx.get(index_url, headers=self._headers, timeout=30)
            if resp.status_code == 200:
                doc_count = self._get_document_count()
                logger.info(
                    "[azure_search] Index '%s' ready (%s documents).",
                    self._index_name,
                    doc_count if doc_count is not None else "unknown",
                )
                return
            if resp.status_code != 404:
                raise RuntimeError(
                    f"Unexpected status {resp.status_code} checking index "
                    f"'{self._index_name}': {resp.text[:200]}"
                )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Could not reach Azure AI Search: {exc}") from exc

        create_url = f"{self._endpoint}/indexes?api-version={_API_VERSION}"
        definition = get_index_definition(self._index_name)
        try:
            resp = httpx.post(
                create_url, json=definition, headers=self._headers, timeout=_REQUEST_TIMEOUT
            )
            if resp.status_code in (200, 201):
                logger.info("[azure_search] Index '%s' created.", self._index_name)
                return
            raise RuntimeError(
                f"Failed to create index '{self._index_name}': "
                f"{resp.status_code} {resp.text[:400]}"
            )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Exception creating index '{self._index_name}': {exc}") from exc

    def _get_document_count(self) -> int | None:
        """Return total documents in the index, or None when the count API fails."""
        count_url = (
            f"{self._endpoint}/indexes/{self._index_name}/docs/$count"
            f"?api-version={_API_VERSION}"
        )
        try:
            resp = httpx.get(count_url, headers=self._headers, timeout=30)
            if resp.status_code == 200:
                return int(resp.text.strip())
        except Exception as exc:
            logger.warning("[azure_search] Could not read document count: %s", exc)
        return None

    def describe_index(self) -> dict:
        """Return index presence, document count, and queryability for diagnostics."""
        index_url = f"{self._endpoint}/indexes/{self._index_name}?api-version={_API_VERSION}"
        search_url = (
            f"{self._endpoint}/indexes/{self._index_name}/docs/search"
            f"?api-version={_API_VERSION}"
        )
        result: dict = {
            "configured": True,
            "endpoint": self._endpoint,
            "index_name": self._index_name,
            "api_version": _API_VERSION,
            "index_exists": False,
            "document_count": None,
            "searchable": False,
            "indexes": [],
        }
        try:
            list_resp = httpx.get(
                f"{self._endpoint}/indexes?api-version={_API_VERSION}",
                headers=self._headers,
                timeout=30,
            )
            if list_resp.status_code == 200:
                result["indexes"] = [
                    item.get("name")
                    for item in list_resp.json().get("value", [])
                    if item.get("name")
                ]
        except Exception as exc:
            result["error"] = f"Could not list indexes: {exc}"
            return result

        try:
            index_resp = httpx.get(index_url, headers=self._headers, timeout=30)
            result["index_exists"] = index_resp.status_code == 200
            if index_resp.status_code != 200:
                result["index_error"] = index_resp.text[:300]
        except Exception as exc:
            result["index_error"] = str(exc)
            return result

        result["document_count"] = self._get_document_count()

        try:
            search_resp = httpx.post(
                search_url,
                headers=self._headers,
                json={"search": "*", "top": 1, "select": "chunk_id"},
                timeout=30,
            )
            result["searchable"] = search_resp.status_code == 200
            if search_resp.status_code != 200:
                result["search_error"] = search_resp.text[:300]
        except Exception as exc:
            result["search_error"] = str(exc)

        return result

    # ── Document upload ───────────────────────────────────────────────────────

    def upload_chunks(self, chunks: list[CourseChunk]) -> dict:
        """
        Serialise and upload chunks to Azure AI Search in batches of 100.

        Each document includes raw_text alongside all vector embeddings so
        the full text is always retrievable from the index without a separate
        blob lookup.

        Returns a summary dict: {"succeeded": int, "failed": int}.
        """
        upload_url = (
            f"{self._endpoint}/indexes/{self._index_name}/docs/index"
            f"?api-version={_API_VERSION}"
        )
        upload_date = datetime.now(timezone.utc).isoformat()
        total_succeeded = 0
        total_failed = 0
        batch_count = (len(chunks) + _BATCH_SIZE - 1) // _BATCH_SIZE

        logger.info(
            "[azure_search] Uploading %d chunks to index '%s' in %d batch(es)...",
            len(chunks), self._index_name, batch_count,
        )

        for batch_start in range(0, len(chunks), _BATCH_SIZE):
            batch = chunks[batch_start : batch_start + _BATCH_SIZE]
            payload = {"value": [_chunk_to_doc(c, upload_date) for c in batch]}

            try:
                resp = httpx.post(
                    upload_url, json=payload, headers=self._headers, timeout=_REQUEST_TIMEOUT
                )
                if resp.status_code in (200, 207):
                    for item in resp.json().get("value", []):
                        if item.get("status"):
                            total_succeeded += 1
                        else:
                            total_failed += 1
                            logger.warning(
                                "[azure_search] Doc failed: key=%s error=%s",
                                item.get("key"), item.get("errorMessage"),
                            )
                else:
                    logger.error(
                        "[azure_search] Batch upload %d returned %d: %s",
                        batch_start, resp.status_code, resp.text[:400],
                    )
                    total_failed += len(batch)
            except Exception as exc:
                logger.error("[azure_search] Exception uploading batch %d: %s", batch_start, exc)
                total_failed += len(batch)

        doc_count = self._get_document_count()
        logger.info(
            "[azure_search] Upload complete — succeeded: %d  failed: %d  "
            "index_total_docs: %s",
            total_succeeded,
            total_failed,
            doc_count if doc_count is not None else "unknown",
        )
        if total_succeeded == 0 and chunks:
            raise RuntimeError(
                f"Azure Search upload failed for all {len(chunks)} chunks "
                f"(index '{self._index_name}')"
            )
        return {"succeeded": total_succeeded, "failed": total_failed}


# ── Serialisation ─────────────────────────────────────────────────────────────

def _chunk_to_doc(chunk: CourseChunk, upload_date: str) -> dict:
    """
    Convert a CourseChunk to an Azure AI Search document.

    Fields stored per document
    --------------------------
    chunk_id        — unique key
    document_id     — parent document identifier
    section_id      — section within the document
    source_file     — original filename (provenance)
    page_num        — first page the chunk appears on (PDF; None for DOCX)
    title           — section heading
    parent_title    — parent section heading
    raw_text        — full verbatim chunk text (always retrievable)
    summary         — LLM-generated 2-sentence summary
    keywords / skills / learning_concepts / learning_outcomes /
    prerequisites / entities / difficulty  — LLM-enriched metadata
    token_count / estimated_read_min / upload_date — stats
    searchable_text — denormalised BM25 catch-all (not retrievable)
    embedding_title / embedding_summary / embedding_content /
    embedding_keywords — 4 × 3072-dim float vectors
    """
    m = chunk.metadata
    doc: dict = {
        "@search.action": "mergeOrUpload",
        # Identity
        "chunk_id":           chunk.chunk_id,
        "document_id":        chunk.document_id,
        "section_id":         chunk.section_id,
        # Source provenance
        "source_file":        chunk.source_file or "",
        # Titles
        "title":              chunk.title or "",
        "parent_title":       chunk.parent_title or "",
        # Raw text — full verbatim content, always retrievable
        "raw_text":           chunk.raw_text or "",
        # LLM-enriched fields
        "summary":            m.summary or "",
        "keywords":           m.keywords or [],
        "skills":             m.skills or [],
        "learning_concepts":  m.learning_concepts or [],
        "learning_outcomes":  m.learning_outcomes or [],
        "prerequisites":      m.prerequisites or [],
        "entities":           m.entities or [],
        "difficulty":         m.difficulty or "introductory",
        # Stats
        "token_count":        chunk.token_count,
        "estimated_read_min": chunk.estimated_read_min,
        "upload_date":        upload_date,
        # BM25 catch-all
        "searchable_text":    chunk.searchable_text or "",
    }
    # Omit nullable / optional fields — Azure rejects null Int32 and empty vectors.
    if chunk.page_num is not None:
        doc["page_num"] = chunk.page_num
    for field_name, vector in (
        ("embedding_title", chunk.embedding_title),
        ("embedding_summary", chunk.embedding_summary),
        ("embedding_content", chunk.embedding_content),
        ("embedding_keywords", chunk.embedding_keywords),
    ):
        if vector:
            doc[field_name] = vector
    return doc
