"""Shared helpers for storage delete and course artifact cleanup."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, status

from lectora_backend.core.course_storage import (
    sanitize_course_slug,
    strip_legacy_outputs_prefix,
)
from lectora_backend.core.job_registry import cancel_jobs_for_storage_delete

logger = logging.getLogger(__name__)

_PIPELINE_COURSES_DIR = (
    Path(__file__).resolve().parents[1] / "pipeline" / "courses"
)
_LEGACY_SHARED_STATE_DIR = (
    Path(__file__).resolve().parents[1] / "pipeline" / "shared_state"
)
_UPLOAD_ROOT = Path(__import__("tempfile").gettempdir()) / "lectora_uploads"


def cancel_background_jobs_for_delete(
    paths: list[str],
    folder_paths: list[str],
) -> list[str]:
    """Signal in-flight A0 / local pipeline jobs that touch deleted storage."""
    cancelled_ids = cancel_jobs_for_storage_delete(paths, folder_paths)
    if not cancelled_ids:
        return cancelled_ids

    from lectora_backend.api.generate_to_job_store import get_generate_to_job_store
    from lectora_backend.api.local_course_job_store import (
        LocalJobStatus,
        get_local_course_job_store,
    )

    reason = "Cancelled — storage deleted while job was running"
    to_store = get_generate_to_job_store()
    for job_id in cancelled_ids:
        to_store.cancel(job_id, reason=reason)

    local_store = get_local_course_job_store()
    for job_id in cancelled_ids:
        job = local_store.get(job_id)
        if job and job.status in (LocalJobStatus.PENDING, LocalJobStatus.PROCESSING):
            local_store.cancel_job(job_id, reason=reason)

    logger.info(
        "[storage_cleanup] Cancelled %s background job(s): %s",
        len(cancelled_ids),
        ", ".join(cancelled_ids[:5]),
    )
    return cancelled_ids


def _azure_configured() -> bool:
    from lectora_backend.config import settings
    return settings.is_azure_storage_configured()


def _uploads_container_name() -> str:
    from lectora_backend.config import settings  # type: ignore[attr-defined]
    from lectora_backend.core.blob_paths import UPLOADED_DOCUMENTS_PREFIX

    return (
        getattr(settings, "uploaded_documents_container_name", None)
        or UPLOADED_DOCUMENTS_PREFIX
    )


def strip_upload_blob_roots(path: str) -> str:
    """Strip optional ``uploaded-documents/`` prefix from a blob path.

    Blobs live as ``{topic}/{file}`` inside the uploads container; this helper
    normalises both the bare container prefix and fully-qualified paths.
    """
    from lectora_backend.core.blob_paths import UPLOADED_DOCUMENTS_PREFIX

    clean = path.strip().lstrip("/")
    if clean.startswith(f"{UPLOADED_DOCUMENTS_PREFIX}/"):
        return clean[len(UPLOADED_DOCUMENTS_PREFIX) + 1 :]
    if clean == UPLOADED_DOCUMENTS_PREFIX:
        return ""
    return clean


# Backward-compatible private alias.
_strip_upload_blob_roots = strip_upload_blob_roots


def delete_course_output_tree(course_title: str) -> int:
    """Delete ``{slug}/`` from Azure and local pipeline course output."""
    import os as _os
    slug = sanitize_course_slug(course_title)
    removed = 0

    if _azure_configured():
        from lectora_backend.repositories.blob_repository import BlobRepository

        repo = BlobRepository()
        removed += repo.delete_blobs_by_prefix(slug)
        removed += repo.delete_blobs_by_prefix(f"outputs/{slug}")

    _courses_base = _PIPELINE_COURSES_DIR.resolve()
    local_dir = (_PIPELINE_COURSES_DIR / slug).resolve()
    if not str(local_dir).startswith(str(_courses_base) + _os.sep):
        logger.error(
            "[storage_cleanup] Refusing to delete outside courses dir: %s", local_dir
        )
        return removed
    if local_dir.is_dir():
        shutil.rmtree(local_dir, ignore_errors=True)
        removed += 1
        logger.info("[storage_cleanup] Removed local output dir %s", local_dir)

    legacy_dir = (_LEGACY_SHARED_STATE_DIR / slug).resolve()
    if legacy_dir.is_dir():
        shutil.rmtree(legacy_dir, ignore_errors=True)
        removed += 1

    return removed


def _resolve_local_upload(path: str) -> Path:
    clean = _strip_upload_blob_roots(path)
    target = (_UPLOAD_ROOT / clean).resolve()
    if _UPLOAD_ROOT.resolve() not in target.parents and target != _UPLOAD_ROOT.resolve():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid path")
    return target


def _resolve_local_artifact(path: str) -> Path:
    clean = strip_legacy_outputs_prefix(path.strip().lstrip("/"))
    if ".." in clean:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid path")
    if clean and (_PIPELINE_COURSES_DIR / clean).exists():
        target = (_PIPELINE_COURSES_DIR / clean).resolve()
        root = _PIPELINE_COURSES_DIR.resolve()
    else:
        target = (_LEGACY_SHARED_STATE_DIR / clean).resolve()
        root = _LEGACY_SHARED_STATE_DIR.resolve()
    if root not in target.parents and target != root:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid path")
    return target


def _course_generation_artifacts_container_name() -> str:
    from lectora_backend.config import settings

    return settings.course_generation_artifacts_container_name


def _generated_courses_container_name() -> str:
    from lectora_backend.config import settings

    return settings.generated_courses_container_name


def delete_storage_file(
    path: str,
    source: Literal[
        "artifacts", "uploads", "course-generation-artifacts", "generated-courses"
    ],
) -> None:
    """Delete one file from Azure Blob and/or local storage."""
    clean = path.strip().lstrip("/")
    if not clean or ".." in clean:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file path",
        )

    removed = False

    if source == "course-generation-artifacts":
        if _azure_configured():
            from lectora_backend.repositories.blob_repository import BlobRepository

            repo = BlobRepository(container_name=_course_generation_artifacts_container_name())
            if repo.exists(clean):
                repo.delete_blob(clean)
                removed = True
    elif source == "generated-courses":
        if _azure_configured():
            from lectora_backend.repositories.blob_repository import BlobRepository

            repo = BlobRepository(container_name=_generated_courses_container_name())
            if repo.exists(clean):
                repo.delete_blob(clean)
                removed = True
        try:
            target = _resolve_local_artifact(path)
            if target.is_file():
                target.unlink(missing_ok=True)
                removed = True
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
    elif source == "uploads":
        blob_path = _strip_upload_blob_roots(path)
        if _azure_configured():
            from lectora_backend.repositories.blob_repository import BlobRepository

            repo = BlobRepository(container_name=_uploads_container_name())
            if repo.exists(blob_path):
                repo.delete_blob(blob_path)
                removed = True
        try:
            target = _resolve_local_upload(path)
            if target.is_file():
                target.unlink(missing_ok=True)
                removed = True
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
    else:
        if _azure_configured():
            from lectora_backend.repositories.blob_repository import BlobRepository

            repo = BlobRepository()
            if repo.exists(clean):
                repo.delete_blob(clean)
                removed = True
        try:
            target = _resolve_local_artifact(path)
            if target.is_file():
                target.unlink(missing_ok=True)
                removed = True
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise

    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        )


def delete_storage_folder(
    folder_path: str,
    source: Literal[
        "artifacts", "uploads", "course-generation-artifacts", "generated-courses"
    ],
) -> int:
    """Delete all blobs/files under a folder prefix. Returns items removed."""
    clean = folder_path.strip().lstrip("/").rstrip("/")
    if not clean or ".." in clean:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid folder path",
        )

    removed = 0

    if source == "course-generation-artifacts":
        prefix = clean if clean.endswith("/") else f"{clean}/"
        if _azure_configured():
            from lectora_backend.repositories.blob_repository import BlobRepository

            removed += BlobRepository(
                container_name=_course_generation_artifacts_container_name()
            ).delete_blobs_by_prefix(prefix)
    elif source == "generated-courses":
        prefix = clean if clean.endswith("/") else f"{clean}/"
        if _azure_configured():
            from lectora_backend.repositories.blob_repository import BlobRepository

            removed += BlobRepository(
                container_name=_generated_courses_container_name()
            ).delete_blobs_by_prefix(prefix)
        try:
            target = _resolve_local_artifact(folder_path)
            if target.is_dir():
                import shutil

                shutil.rmtree(target, ignore_errors=True)
                removed += 1
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
    elif source == "uploads":
        prefix = clean if clean.endswith("/") else f"{clean}/"
        blob_prefix = _strip_upload_blob_roots(prefix)
        if _azure_configured():
            from lectora_backend.repositories.blob_repository import BlobRepository

            removed += BlobRepository(
                container_name=_uploads_container_name()
            ).delete_blobs_by_prefix(blob_prefix)
        local_dir = (_UPLOAD_ROOT / _strip_upload_blob_roots(clean)).resolve()
        if local_dir.is_dir():
            shutil.rmtree(local_dir, ignore_errors=True)
            removed += 1
            logger.info("[storage_cleanup] Removed upload folder %s", local_dir)
    else:
        import os as _os
        rel = strip_legacy_outputs_prefix(clean).strip("/")
        if _azure_configured():
            from lectora_backend.repositories.blob_repository import BlobRepository

            repo = BlobRepository()
            removed += repo.delete_blobs_by_prefix(rel)
            if rel:
                removed += repo.delete_blobs_by_prefix(f"outputs/{rel}")
        _courses_base = _PIPELINE_COURSES_DIR.resolve()
        local_dir = (_PIPELINE_COURSES_DIR / rel).resolve() if rel else _courses_base
        if str(local_dir) != str(_courses_base) and not str(local_dir).startswith(str(_courses_base) + _os.sep):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid folder path")
        if local_dir.is_dir():
            shutil.rmtree(local_dir, ignore_errors=True)
            removed += 1
        _legacy_base = _LEGACY_SHARED_STATE_DIR.resolve()
        legacy_dir = (_LEGACY_SHARED_STATE_DIR / rel).resolve() if rel else _legacy_base
        if str(legacy_dir) != str(_legacy_base) and not str(legacy_dir).startswith(str(_legacy_base) + _os.sep):
            pass  # silently skip invalid legacy path
        elif legacy_dir.is_dir():
            shutil.rmtree(legacy_dir, ignore_errors=True)
            removed += 1

    if removed == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Folder not found or empty: {folder_path}",
        )
    return removed
