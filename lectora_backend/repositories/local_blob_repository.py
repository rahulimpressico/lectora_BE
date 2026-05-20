"""Local-filesystem drop-in replacement for BlobRepository.

Used by dev_app.py so the full pipeline can run without Azure Blob Storage.
Blob paths that look like absolute local paths (e.g. /tmp/lectora_uploads/...)
are accessed directly; all other paths are resolved relative to a local base dir.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


_DEFAULT_BASE = Path(tempfile.gettempdir()) / "lectora_dev_blobs"


class LocalBlobRepository:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base = Path(base_dir or _DEFAULT_BASE)
        self._base.mkdir(parents=True, exist_ok=True)

    def _resolve(self, blob_path: str) -> Path:
        """Return the filesystem path for a blob path.

        Absolute paths that already exist on disk are used as-is (covers
        study-guide uploads saved by the generate-to endpoint to /tmp).
        All other paths are resolved under self._base.
        """
        p = Path(blob_path)
        if p.is_absolute() and p.exists():
            return p
        # Treat as relative to base, strip leading slash first
        rel = Path(blob_path.lstrip("/\\"))
        full = self._base / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        return full

    def upload_text(self, blob_path: str, content: str) -> None:
        self._resolve(blob_path).write_text(content, encoding="utf-8")

    def download_bytes(self, blob_path: str) -> bytes:
        p = self._resolve(blob_path)
        if not p.exists():
            # Last resort: try the path as-is even if it wasn't absolute
            abs_p = Path(blob_path)
            if abs_p.exists():
                return abs_p.read_bytes()
            raise FileNotFoundError(f"Local blob not found: {blob_path!r}")
        return p.read_bytes()

    def download_text(self, blob_path: str) -> str:
        return self.download_bytes(blob_path).decode("utf-8")

    def upload_file(
        self,
        local_path: str,
        blob_path: str,
        content_type: str | None = None,
    ) -> None:
        self._resolve(blob_path).write_bytes(Path(local_path).read_bytes())

    def upload_bytes(
        self,
        blob_path: str,
        content: bytes,
        content_type: str | None = None,
    ) -> None:
        self._resolve(blob_path).write_bytes(content)

    def exists(self, blob_path: str) -> bool:
        p = self._resolve(blob_path)
        if p.exists():
            return True
        return Path(blob_path).exists()

    def delete_blob(self, blob_path: str) -> None:
        p = self._resolve(blob_path)
        if p.exists():
            p.unlink(missing_ok=True)

    def list_blobs(self, prefix: str) -> list[str]:
        prefix_path = self._base / prefix.lstrip("/\\")
        if not prefix_path.exists():
            return []
        return [
            str(p.relative_to(self._base)).replace(os.sep, "/")
            for p in prefix_path.rglob("*")
            if p.is_file()
        ]

    def prefix_exists(self, prefix: str) -> bool:
        return bool(self.list_blobs(prefix))
