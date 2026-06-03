"""
Asset Library routes — browse pipeline artifacts and uploaded source documents.

GET /storage/browse?prefix=           — pipeline artifacts (local shared_state or Azure)
GET /storage/uploaded-documents/browse?prefix=   — uploaded-documents/ in container
GET /storage/file?path=&source=       — preview / download (local or Azure)
"""
from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse, Response

from pydantic import BaseModel, Field, model_validator

from lectora_backend.core.blob_paths import UPLOADED_DOCUMENTS_PREFIX
from lectora_backend.repositories.blob_repository import BlobRepository

logger = logging.getLogger(__name__)
router = APIRouter()

_LOCAL_COURSES_DIR = Path(__file__).resolve().parents[2] / "pipeline" / "courses"
_LOCAL_LEGACY_DIR = Path(__file__).resolve().parents[2] / "pipeline" / "shared_state"
_LOCAL_DIR = _LOCAL_COURSES_DIR
_LOCAL_COURSES_DIR.mkdir(parents=True, exist_ok=True)
_UPLOAD_ROOT = Path(tempfile.gettempdir()) / "lectora_uploads"
# Only these appear on the Documents page (not generated_to.json etc.)
_UPLOAD_DOC_EXTENSIONS = frozenset({".docx", ".doc", ".pdf"})

_MIME: dict[str, str] = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".json": "application/json",
    ".pdf":  "application/pdf",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".txt":  "text/plain",
    ".csv":  "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip":  "application/zip",
}


class StorageEntry(BaseModel):
    name: str
    path: str
    entryType: Literal["folder", "file"]
    size: int | None = None
    lastModified: str | None = None
    contentType: str | None = None
    fileCount: int | None = None
    extension: str | None = None


class BrowseResponse(BaseModel):
    prefix: str
    entries: list[StorageEntry]
    totalFiles: int
    totalFolders: int
    totalSize: int
    source: Literal["azure", "local"]
    container_name: str | None = Field(default=None, alias="containerName")


class DeleteStorageFilesRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=100)
    folder_paths: list[str] = Field(default_factory=list, alias="folderPaths", max_length=50)
    source: Literal["artifacts", "uploads"] = "uploads"

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _require_targets(self) -> "DeleteStorageFilesRequest":
        if not self.paths and not self.folder_paths:
            msg = "At least one path or folderPath is required."
            raise ValueError(msg)
        return self


class DeleteStorageFileResult(BaseModel):
    path: str
    ok: bool
    error: str | None = None


class DeleteStorageFilesResponse(BaseModel):
    results: list[DeleteStorageFileResult]
    deleted_count: int = Field(alias="deletedCount")

    model_config = {"populate_by_name": True}


def _iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _main_container_name() -> str:
    from lectora_backend.config import settings  # type: ignore[attr-defined]

    return getattr(settings, "blob_container_name", None) or "regedlectoraaistorage"


def _uploads_container_display_name() -> str:
    return _uploads_container_name()


def _azure_configured() -> bool:
    try:
        from lectora_backend.config import settings  # type: ignore[attr-defined]
        return bool(getattr(settings, "azure_storage_connection_string", "").strip())
    except Exception:
        return False


def _uploads_container_name() -> str:
    from lectora_backend.config import settings  # type: ignore[attr-defined]

    return (
        getattr(settings, "uploaded_documents_container_name", None)
        or UPLOADED_DOCUMENTS_PREFIX
    )


def _uploads_blob_repo() -> BlobRepository:
    return BlobRepository(container_name=_uploads_container_name())


def _strip_upload_blob_roots(path: str) -> str:
    """Strip optional ``uploaded-documents/`` prefix; blobs live as ``{topic}/{file}``."""
    clean = path.strip().lstrip("/")
    if clean.startswith(f"{UPLOADED_DOCUMENTS_PREFIX}/"):
        return clean[len(UPLOADED_DOCUMENTS_PREFIX) + 1 :]
    if clean == UPLOADED_DOCUMENTS_PREFIX:
        return ""
    return clean


def _relative_upload_prefix(relative: str) -> str:
    clean = _strip_upload_blob_roots(relative).strip("/")
    if not clean:
        return ""
    return f"{clean}/"


def _local_uploaded_documents_relative(prefix: str) -> str:
    return _strip_upload_blob_roots(prefix)


def _is_upload_document(name: str) -> bool:
    return Path(name).suffix.lower() in _UPLOAD_DOC_EXTENSIONS


def _browse_uploaded_documents_azure(relative_prefix: str) -> BrowseResponse:
    """List blobs in the dedicated ``uploaded-documents`` Azure container."""
    azure_prefix = _relative_upload_prefix(relative_prefix)
    uploads_repo = _uploads_blob_repo()
    primary = _filter_upload_entries(
        _azure_browse_container(uploads_repo, azure_prefix, uploads_only=True),
    )
    return BrowseResponse(
        prefix=azure_prefix,
        entries=primary.entries,
        totalFiles=primary.totalFiles,
        totalFolders=primary.totalFolders,
        totalSize=primary.totalSize,
        source="azure",
        container_name=_uploads_container_display_name(),
    )


def _filter_upload_entries(response: BrowseResponse) -> BrowseResponse:
    """Hide non-document files (e.g. generated_to.json) from the Documents library."""
    entries = [
        e for e in response.entries
        if e.entryType == "folder" or _is_upload_document(e.name)
    ]
    files = [e for e in entries if e.entryType == "file"]
    folders = [e for e in entries if e.entryType == "folder"]
    total_size = sum(e.size or 0 for e in files)
    return BrowseResponse(
        prefix=response.prefix,
        entries=entries,
        totalFiles=len(files),
        totalFolders=len(folders),
        totalSize=total_size,
        source=response.source,
        container_name=response.container_name,
    )


def _normalize_blob_path(path: str, source: Literal["artifacts", "uploads"]) -> str:
    clean = path.strip().lstrip("/")
    if ".." in clean or clean.startswith("/"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid path")
    if source == "uploads":
        return _strip_upload_blob_roots(clean)
    return clean


def _azure_browse(prefix: str, *, uploads_only: bool = False) -> BrowseResponse:
    return _azure_browse_container(BlobRepository(), prefix, uploads_only=uploads_only)


def _azure_browse_container(
    repo: BlobRepository,
    prefix: str,
    *,
    uploads_only: bool = False,
) -> BrowseResponse:
    container = repo._container_client  # noqa: SLF001

    seen_folders: dict[str, int] = {}
    direct_files: list[tuple[str, int | None, str | None]] = []

    for blob in container.list_blobs(name_starts_with=prefix):
        name = blob.name
        if not name.startswith(prefix):
            continue
        relative = name[len(prefix) :]
        parts = [p for p in relative.split("/") if p]
        if not parts:
            continue
        lm = (
            blob.last_modified.astimezone(timezone.utc).isoformat()
            if getattr(blob, "last_modified", None)
            else None
        )
        size = int(blob.size) if getattr(blob, "size", None) is not None else None
        if len(parts) == 1:
            if not uploads_only or _is_upload_document(parts[0]):
                direct_files.append((name, size, lm))
        else:
            fp = prefix + parts[0] + "/"
            seen_folders[fp] = seen_folders.get(fp, 0) + 1

    entries: list[StorageEntry] = []
    total_size = 0
    for fp, count in sorted(seen_folders.items()):
        folder_name = fp.rstrip("/").rsplit("/", 1)[-1]
        entries.append(StorageEntry(
            name=folder_name,
            path=fp,
            entryType="folder",
            fileCount=count,
        ))
    for bp, size, lm in sorted(direct_files, key=lambda x: x[0]):
        file_name = bp.rsplit("/", 1)[-1]
        ext = Path(file_name).suffix.lower()
        if size:
            total_size += size
        entries.append(StorageEntry(
            name=file_name,
            path=bp,
            entryType="file",
            size=size,
            lastModified=lm,
            contentType=_MIME.get(ext, "application/octet-stream"),
            extension=ext,
        ))

    files = [e for e in entries if e.entryType == "file"]
    folders = [e for e in entries if e.entryType == "folder"]
    return BrowseResponse(
        prefix=prefix,
        entries=entries,
        totalFiles=len(files),
        totalFolders=len(folders),
        totalSize=total_size,
        source="azure",
        container_name=_main_container_name(),
    )


def _resolve_local_file(source: Literal["artifacts", "uploads"], relative_path: str) -> Path:
    clean = relative_path.strip().lstrip("/")
    if source == "uploads":
        base = _UPLOAD_ROOT.resolve()
        rel = _local_uploaded_documents_relative(clean)
    else:
        from lectora_backend.core.course_storage import strip_legacy_outputs_prefix

        rel = strip_legacy_outputs_prefix(clean)
        if rel and (_LOCAL_COURSES_DIR / rel).exists():
            base = _LOCAL_COURSES_DIR.resolve()
        elif (_LOCAL_COURSES_DIR / rel).exists() or not (_LOCAL_LEGACY_DIR / rel).exists():
            base = _LOCAL_COURSES_DIR.resolve()
        else:
            base = _LOCAL_LEGACY_DIR.resolve()

    target = (base / rel).resolve() if rel else base.resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid path")
    if not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return target


def _local_browse_at(base: Path, relative_prefix: str) -> BrowseResponse:
    base.mkdir(parents=True, exist_ok=True)
    clean = relative_prefix.strip("/")
    target = (base / clean).resolve() if clean else base.resolve()

    if not str(target).startswith(str(base.resolve())):
        return BrowseResponse(
            prefix=relative_prefix, entries=[], totalFiles=0,
            totalFolders=0, totalSize=0, source="local",
        )
    if not target.exists():
        return BrowseResponse(
            prefix=relative_prefix, entries=[], totalFiles=0,
            totalFolders=0, totalSize=0, source="local",
        )

    entries: list[StorageEntry] = []
    total_size = 0
    items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    for item in items:
        rel = str(item.relative_to(base)).replace("\\", "/")
        if item.is_dir():
            fc = sum(1 for f in item.rglob("*") if f.is_file())
            sz = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
            total_size += sz
            entries.append(StorageEntry(
                name=item.name,
                path=rel + "/",
                entryType="folder",
                lastModified=_iso(item),
                fileCount=fc,
                size=sz,
            ))
        else:
            stat = item.stat()
            ext = item.suffix.lower()
            total_size += stat.st_size
            entries.append(StorageEntry(
                name=item.name,
                path=rel,
                entryType="file",
                size=stat.st_size,
                lastModified=_iso(item),
                contentType=_MIME.get(ext, "application/octet-stream"),
                extension=ext,
            ))

    files = [e for e in entries if e.entryType == "file"]
    folders = [e for e in entries if e.entryType == "folder"]
    return BrowseResponse(
        prefix=relative_prefix,
        entries=entries,
        totalFiles=len(files),
        totalFolders=len(folders),
        totalSize=total_size,
        source="local",
        container_name=_main_container_name() if base == _LOCAL_COURSES_DIR.resolve() else None,
    )


def _local_browse_artifacts(relative_prefix: str) -> BrowseResponse:
    """Browse local pipeline courses + legacy shared_state (mirrors Azure container layout)."""
    from lectora_backend.core.course_storage import strip_legacy_outputs_prefix

    clean = strip_legacy_outputs_prefix(relative_prefix.strip().lstrip("/"))

    if clean:
        legacy_resp = _local_browse_at(_LOCAL_LEGACY_DIR, clean)
        if legacy_resp.entries:
            legacy_resp.container_name = _main_container_name()
            return legacy_resp
        out_resp = _local_browse_at(_LOCAL_COURSES_DIR, clean)
        out_resp.container_name = _main_container_name()
        return out_resp

    entries: list[StorageEntry] = []
    seen: set[str] = set()
    if _LOCAL_COURSES_DIR.exists():
        for item in sorted(
            _LOCAL_COURSES_DIR.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        ):
            if not item.is_dir():
                continue
            seen.add(item.name)
            fc = sum(1 for f in item.rglob("*") if f.is_file())
            entries.append(
                StorageEntry(
                    name=item.name,
                    path=f"{item.name}/",
                    entryType="folder",
                    fileCount=fc,
                )
            )
    if _LOCAL_LEGACY_DIR.exists():
        for item in sorted(
            _LOCAL_LEGACY_DIR.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        ):
            if not item.is_dir() or item.name in seen:
                continue
            fc = sum(1 for f in item.rglob("*") if f.is_file())
            entries.append(
                StorageEntry(
                    name=item.name,
                    path=f"{item.name}/",
                    entryType="folder",
                    fileCount=fc,
                )
            )

    folders = [e for e in entries if e.entryType == "folder"]
    return BrowseResponse(
        prefix="",
        entries=entries,
        totalFiles=0,
        totalFolders=len(folders),
        totalSize=0,
        source="local",
        container_name=_main_container_name(),
    )


def _download_azure_blob(blob_path: str) -> tuple[bytes, str]:
    from lectora_backend.repositories.blob_repository import BlobRepository

    try:
        data = BlobRepository().download_bytes(blob_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Blob not found: {blob_path}",
        ) from exc
    ext = Path(blob_path).suffix.lower()
    media = _MIME.get(ext, "application/octet-stream")
    return data, media


def _try_azure_file(blob_path: str) -> tuple[bytes, str] | None:
    """Return file bytes from default artifacts container when blob exists."""
    if not _azure_configured():
        return None
    try:
        return _download_azure_blob(blob_path)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return None
        raise


def _try_azure_upload_file(blob_path: str) -> tuple[bytes, str] | None:
    """Return file bytes from the dedicated uploaded-documents container."""
    if not _azure_configured():
        return None
    normalized = _strip_upload_blob_roots(blob_path)
    try:
        return _download_azure_blob_from_repo(_uploads_blob_repo(), normalized)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return None
        raise


def _download_azure_blob_from_repo(repo: BlobRepository, blob_path: str) -> tuple[bytes, str]:
    try:
        data = repo.download_bytes(blob_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Blob not found: {blob_path}",
        ) from exc
    ext = Path(blob_path).suffix.lower()
    media = _MIME.get(ext, "application/octet-stream")
    return data, media


def _file_response_bytes(data: bytes, blob_path: str) -> Response:
    ext = Path(blob_path).suffix.lower()
    media = _MIME.get(ext, "application/octet-stream")
    filename = Path(blob_path).name
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


def _artifacts_browse_prefix(prefix: str) -> str:
    """Map UI prefix to a blob prefix inside the main container (``regedlectoraaistorage``)."""
    from lectora_backend.core.course_storage import strip_legacy_outputs_prefix

    clean = strip_legacy_outputs_prefix(prefix.strip().lstrip("/"))
    if not clean:
        return ""
    return clean if clean.endswith("/") else f"{clean}/"


@router.get("/browse", response_model=BrowseResponse)
async def browse_storage(
    prefix: str = Query(default="", description="Path prefix to browse. Empty for root."),
) -> BrowseResponse:
    """Browse the main Azure container (``regedlectoraaistorage``) — full tree at root."""
    azure_prefix = _artifacts_browse_prefix(prefix)
    if _azure_configured():
        try:
            result = _azure_browse(azure_prefix)
            if result.entries or prefix:
                return result
            logger.info(
                "[storage] Azure browse empty at prefix=%r — falling back to local.",
                azure_prefix,
            )
        except Exception as exc:
            logger.warning("[storage] Azure browse failed (%s), falling back to local.", exc)
    return _local_browse_artifacts(prefix)


@router.get("/uploaded-documents/browse", response_model=BrowseResponse)
async def browse_uploaded_documents(
    prefix: str = Query(
        default="",
        description=(
            "Path within uploaded-documents/ "
            "(e.g. empty for root, or course-topic/ for a folder)."
        ),
    ),
) -> BrowseResponse:
    """
    Browse source documents in the dedicated Azure container ``uploaded-documents``,
    or local temp in dev.
    """
    if _azure_configured():
        try:
            primary = _browse_uploaded_documents_azure(prefix)
            filtered = _filter_upload_entries(primary)
            if prefix or filtered.totalFiles or filtered.totalFolders:
                return filtered
            logger.info(
                "[storage] uploaded-documents empty at root — listing main container %s",
                _main_container_name(),
            )
            main_docs = _filter_upload_entries(_azure_browse("", uploads_only=True))
            main_docs.container_name = _main_container_name()
            return main_docs
        except Exception as exc:
            logger.warning(
                "[storage] Azure uploaded-documents browse failed (%s), falling back to local.",
                exc,
            )
    _UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    local = _filter_upload_entries(
        _local_browse_at(_UPLOAD_ROOT, _local_uploaded_documents_relative(prefix)),
    )
    if prefix or local.totalFiles or local.totalFolders:
        local.container_name = _uploads_container_display_name()
        return local
    return _filter_upload_entries(_local_browse_artifacts(prefix))


@router.get("/categories/{category}/browse", response_model=BrowseResponse)
async def browse_by_category(
    category: str,
    prefix: str = Query(default="", description="Path prefix to browse. Empty for root."),
) -> BrowseResponse:
    """Browse storage by named category.

    Categories:
      source-documents    — uploaded source DOCX/PDF files (uploaded-documents container)
      generated-courses   — final generated DOCX outputs (generated-courses container)
      pipeline-artifacts  — all pipeline artifacts (main artifacts container)
      test-data           — same as pipeline-artifacts (fallback)
    """
    if category == "source-documents":
        if _azure_configured():
            try:
                primary = _browse_uploaded_documents_azure(prefix)
                filtered = _filter_upload_entries(primary)
                if prefix or filtered.totalFiles or filtered.totalFolders:
                    return filtered
            except Exception as exc:
                logger.warning("[storage/categories] source-documents Azure failed (%s), using local.", exc)
        _UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        local = _filter_upload_entries(
            _local_browse_at(_UPLOAD_ROOT, _local_uploaded_documents_relative(prefix))
        )
        local.container_name = _uploads_container_display_name()
        return local

    if category == "generated-courses":
        if _azure_configured():
            try:
                from lectora_backend.config import settings as _settings
                _gc_container = getattr(_settings, "generated_courses_container_name", "generated-courses")
                _gc_repo = BlobRepository(container_name=_gc_container)
                return _azure_browse_container(_gc_repo, prefix.lstrip("/"))
            except Exception as exc:
                logger.warning("[storage/categories] generated-courses Azure failed (%s), using local.", exc)
        # Local dev: browse pipeline/courses/ and return only output DOCX files
        local = _local_browse_artifacts(prefix)
        local.container_name = "generated-courses (local)"
        return local

    # pipeline-artifacts and test-data → main artifacts container
    azure_prefix = _artifacts_browse_prefix(prefix)
    if _azure_configured():
        try:
            result = _azure_browse(azure_prefix)
            if result.entries or prefix:
                return result
        except Exception as exc:
            logger.warning("[storage/categories] %s Azure failed (%s), using local.", category, exc)
    return _local_browse_artifacts(prefix)


@router.get("/file", summary="Download or preview a file")
async def get_storage_file(
    path: str = Query(..., description="Blob path from browse response"),
    source: Literal["artifacts", "uploads"] = Query(
        default="artifacts",
        description="artifacts = pipeline output; uploads = uploaded-documents/ in blob",
    ),
) -> Response:
    """Serve file bytes for in-browser preview (images, JSON, DOCX) or download."""
    if source == "uploads":
        blob_path = _normalize_blob_path(path, source)
        azure_result = _try_azure_upload_file(blob_path)
        if azure_result is not None:
            data, _ = azure_result
            return _file_response_bytes(data, blob_path)
        target = _resolve_local_file(source, path)
        ext = target.suffix.lower()
        media = _MIME.get(ext, "application/octet-stream")
        return FileResponse(path=str(target), media_type=media, filename=target.name)

    # Artifacts: try Azure when listed from blob; fall back to local shared_state.
    blob_path = path.strip().lstrip("/")
    azure_result = _try_azure_file(blob_path)
    if azure_result is not None:
        data, _ = azure_result
        return _file_response_bytes(data, blob_path)

    target = _resolve_local_file(source, path)
    ext = target.suffix.lower()
    media = _MIME.get(ext, "application/octet-stream")
    return FileResponse(path=str(target), media_type=media, filename=target.name)


@router.post("/delete", response_model=DeleteStorageFilesResponse)
async def delete_storage_files(
    body: DeleteStorageFilesRequest,
) -> DeleteStorageFilesResponse:
    """Delete files and/or folders from blob storage."""
    from lectora_backend.core.storage_cleanup import (
        cancel_background_jobs_for_delete,
        delete_storage_file,
        delete_storage_folder,
    )

    cancelled = cancel_background_jobs_for_delete(body.paths, body.folder_paths)
    if cancelled:
        logger.info(
            "[storage] Cancelled %s in-flight job(s) before delete",
            len(cancelled),
        )

    results: list[DeleteStorageFileResult] = []
    deleted_count = 0

    for path in body.paths:
        try:
            delete_storage_file(path, body.source)
            results.append(DeleteStorageFileResult(path=path, ok=True))
            deleted_count += 1
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            results.append(DeleteStorageFileResult(path=path, ok=False, error=detail))
        except Exception as exc:
            logger.exception("[storage] Delete failed for %s", path)
            results.append(DeleteStorageFileResult(path=path, ok=False, error=str(exc)))

    for folder_path in body.folder_paths:
        try:
            count = delete_storage_folder(folder_path, body.source)
            results.append(
                DeleteStorageFileResult(
                    path=folder_path,
                    ok=True,
                    error=None if count else "Folder was empty",
                ),
            )
            deleted_count += max(count, 1)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            results.append(DeleteStorageFileResult(path=folder_path, ok=False, error=detail))
        except Exception as exc:
            logger.exception("[storage] Folder delete failed for %s", folder_path)
            results.append(
                DeleteStorageFileResult(path=folder_path, ok=False, error=str(exc)),
            )

    return DeleteStorageFilesResponse(results=results, deleted_count=deleted_count)
