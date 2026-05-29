"""Track in-flight jobs so storage deletes can cancel and clean up resources."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from lectora_backend.core.course_storage import storage_path_matches

logger = logging.getLogger(__name__)


@dataclass
class GenerateTOJobHandle:
    job_id: str
    blob_paths: list[str]
    course_folder: str | None
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class LocalPipelineJobHandle:
    job_id: str
    course_title: str
    course_slug: str
    blob_paths: list[str]
    cancel_event: threading.Event = field(default_factory=threading.Event)


_lock = threading.Lock()
_generate_to_jobs: dict[str, GenerateTOJobHandle] = {}
_local_pipeline_jobs: dict[str, LocalPipelineJobHandle] = {}


def register_generate_to(
    job_id: str,
    *,
    blob_paths: list[str],
    course_folder: str | None = None,
) -> GenerateTOJobHandle:
    handle = GenerateTOJobHandle(
        job_id=job_id,
        blob_paths=list(blob_paths),
        course_folder=course_folder,
    )
    with _lock:
        _generate_to_jobs[job_id] = handle
    return handle


def unregister_generate_to(job_id: str) -> None:
    with _lock:
        _generate_to_jobs.pop(job_id, None)


def get_generate_to(job_id: str) -> GenerateTOJobHandle | None:
    with _lock:
        return _generate_to_jobs.get(job_id)


def register_local_pipeline(
    job_id: str,
    *,
    course_title: str,
    course_slug: str,
    blob_paths: list[str] | None = None,
) -> LocalPipelineJobHandle:
    handle = LocalPipelineJobHandle(
        job_id=job_id,
        course_title=course_title,
        course_slug=course_slug,
        blob_paths=list(blob_paths or []),
    )
    with _lock:
        _local_pipeline_jobs[job_id] = handle
    return handle


def unregister_local_pipeline(job_id: str) -> None:
    with _lock:
        _local_pipeline_jobs.pop(job_id, None)


def get_local_pipeline(job_id: str) -> LocalPipelineJobHandle | None:
    with _lock:
        return _local_pipeline_jobs.get(job_id)


def _prefixes_from_delete(paths: list[str], folder_paths: list[str]) -> set[str]:
    prefixes: set[str] = set()
    for p in paths:
        prefixes.add(p.strip().lstrip("/"))
    for folder in folder_paths:
        f = folder.strip().lstrip("/").rstrip("/")
        if f:
            prefixes.add(f)
    return prefixes


def cancel_jobs_for_storage_delete(
    paths: list[str],
    folder_paths: list[str],
) -> list[str]:
    """Cancel in-flight jobs touching deleted paths. Returns cancelled job ids."""
    prefixes = _prefixes_from_delete(paths, folder_paths)
    if not prefixes:
        return []

    cancelled: list[str] = []

    with _lock:
        for job_id, handle in list(_generate_to_jobs.items()):
            targets = list(handle.blob_paths)
            if handle.course_folder:
                targets.append(handle.course_folder)
            if any(storage_path_matches(prefixes, t) for t in targets):
                handle.cancel_event.set()
                cancelled.append(job_id)
                logger.debug(
                    "[job_registry] Signalled cancel for generate-to %s", job_id
                )
                logger.info(
                    "[job_registry] Cancelled generate-to job %s (storage delete)",
                    job_id,
                )

        for job_id, handle in list(_local_pipeline_jobs.items()):
            targets = list(handle.blob_paths) + [handle.course_slug]
            if any(storage_path_matches(prefixes, t) for t in targets):
                handle.cancel_event.set()
                cancelled.append(job_id)
                logger.debug(
                    "[job_registry] Signalled cancel for generate-to %s", job_id
                )
                logger.info(
                    "[job_registry] Cancelled local pipeline job %s (storage delete)",
                    job_id,
                )

    return cancelled
