"""
In-memory store for per-document ingestion status.

Tracks the lifecycle of background ingestion tasks triggered after upload:
  pending   → ingestion queued but not yet started
  processing → ingestion pipeline is running
  indexed   → successfully embedded and indexed in Azure AI Search
  parsed    → parsed/chunked but search indexing was skipped (no Azure Search config)
  failed    → ingestion encountered an unrecoverable error

Entries expire after TTL_SECONDS to avoid unbounded memory growth.
"""
from __future__ import annotations

import time
import threading
from typing import Literal

IngestionStatus = Literal["pending", "processing", "indexed", "parsed", "failed"]

_TTL_SECONDS = 4 * 3600  # 4 hours

_store: dict[str, dict] = {}
_lock = threading.Lock()


def set_status(
    document_id: str,
    status: IngestionStatus,
    total_chunks: int = 0,
    error: str | None = None,
) -> None:
    with _lock:
        _store[document_id] = {
            "document_id": document_id,
            "status": status,
            "total_chunks": total_chunks,
            "error": error,
            "updated_at": time.time(),
        }


def get_status(document_id: str) -> dict | None:
    with _lock:
        entry = _store.get(document_id)
        if entry is None:
            return None
        if time.time() - entry["updated_at"] > _TTL_SECONDS:
            del _store[document_id]
            return None
        return dict(entry)
