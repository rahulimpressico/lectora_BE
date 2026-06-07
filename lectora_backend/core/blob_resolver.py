"""
Shared blob-path → local-filesystem resolution.

Consumed by:
  - local_jobs.py  (POST /jobs): resolves blob paths before handing to pipeline agents
  - generate_to.py (POST /documents/generate-to): caches Azure blobs locally after download

Resolution order
----------------
1. Already-absolute path that exists on disk → return as-is.
2. Strip optional ``uploaded-documents/`` container prefix.
3. Check local cache at ``_UPLOAD_ROOT / normalized``.
4. If Azure storage is configured, download the blob and **persist** it to
   ``_UPLOAD_ROOT / normalized`` so that subsequent calls (e.g. POST /jobs after
   POST /documents/generate-to) find it without a second Azure round-trip.
5. Return ``None`` if the file cannot be located anywhere.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Mirrors generate_to.py / storage.py so all modules share one temp root.
_UPLOAD_ROOT = Path(tempfile.gettempdir()) / "lectora_uploads"
_UPLOAD_PREFIX = "uploaded-documents"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def strip_upload_prefix(path: str) -> str:
    """Remove the ``uploaded-documents/`` container prefix when present.

    Examples
    --------
    >>> strip_upload_prefix("uploaded-documents/Flood/sg.docx")
    'Flood/sg.docx'
    >>> strip_upload_prefix("Flood/sg.docx")
    'Flood/sg.docx'
    """
    clean = path.strip().lstrip("/")
    prefix = f"{_UPLOAD_PREFIX}/"
    if clean.startswith(prefix):
        return clean[len(prefix):]
    if clean == _UPLOAD_PREFIX:
        return ""
    return clean


def _azure_ready() -> bool:
    from lectora_backend.config import settings
    return settings.is_azure_storage_configured()


def _uploads_repo():
    """Return a BlobRepository targeting the uploaded-documents container."""
    from lectora_backend.repositories.blob_repository import BlobRepository
    try:
        from lectora_backend.config import settings  # type: ignore[attr-defined]
        container = getattr(settings, "uploaded_documents_container_name", None) or _UPLOAD_PREFIX
    except Exception:
        container = _UPLOAD_PREFIX
    return BlobRepository(container_name=container)


# ─── Primary resolver ─────────────────────────────────────────────────────────

def resolve_blob_to_local(blob_path: str) -> Path | None:
    """Resolve *blob_path* to an absolute local :class:`~pathlib.Path`.

    Parameters
    ----------
    blob_path:
        A blob path as returned by ``POST /documents/upload`` (relative, e.g.
        ``"Long_Term_course/sg.docx"``) **or** an Azure browser path that may
        carry the ``uploaded-documents/`` prefix.

    Returns
    -------
    Path | None
        Absolute local path to a readable file, or ``None`` if the file cannot
        be located either locally or in Azure Blob Storage.
    """
    _UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

    clean = blob_path.strip().lstrip("/")
    normalized = strip_upload_prefix(clean)

    # ── 1. Absolute path already on disk ────────────────────────────────────
    if Path(clean).is_absolute():
        p = Path(clean)
        if p.is_file():
            logger.debug("[blob_resolver] absolute hit: %s", p)
            return p
        logger.warning("[blob_resolver] absolute path missing: %s", clean)
        return None

    # ── 2. Local cache hit ────────────────────────────────────────────────────
    local = (_UPLOAD_ROOT / normalized).resolve()
    # Safety: stay inside _UPLOAD_ROOT
    if not str(local).startswith(str(_UPLOAD_ROOT.resolve())):
        logger.warning("[blob_resolver] path traversal rejected: %r", blob_path)
        return None

    if local.is_file():
        logger.debug("[blob_resolver] local hit: %r → %s", blob_path, local)
        return local

    # ── 3. Azure download → persist to local cache (atomic write to prevent TOCTOU) ─
    if _azure_ready():
        try:
            data = _uploads_repo().download_bytes(normalized)
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_suffix(local.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(local)  # atomic rename — readers always see a complete file
            logger.info(
                "[blob_resolver] Azure→local: %r → %s (%d bytes)",
                blob_path,
                local,
                len(data),
            )
            return local
        except FileNotFoundError:
            logger.warning(
                "[blob_resolver] not in Azure: %r (normalized: %r)", blob_path, normalized
            )
        except Exception as exc:
            logger.warning(
                "[blob_resolver] Azure error for %r: %s", blob_path, exc
            )

    logger.error(
        "[blob_resolver] not found: blob_path=%r normalized=%r expected_local=%s",
        blob_path,
        normalized,
        local,
    )
    return None
