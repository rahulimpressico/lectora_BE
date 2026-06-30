"""
Read / resolve pipeline artifacts from the Azure ``course-generation-artifacts`` container.

When Azure is configured, job APIs and the Asset Library prefer this container
over local ``pipeline/courses`` / ``pipeline/shared_state`` files.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from lectora_backend.repositories.blob_repository import BlobRepository

logger = logging.getLogger(__name__)

_SHARED_STATE_FILENAMES = (
    "pipeline_shared_state.json",
    "shared_state.json",
)

# job_id -> artifact root prefix (ends with "/"), populated after first resolve
_JOB_ROOT_CACHE: dict[str, str] = {}
_JOB_ROOT_CACHE_LOCK = threading.Lock()


def is_azure_artifacts_enabled() -> bool:
    from lectora_backend.config import settings

    return settings.is_azure_storage_configured()


def artifacts_container_name() -> str:
    from lectora_backend.config import settings

    return settings.course_generation_artifacts_container_name


def _repo() -> BlobRepository:
    return BlobRepository(container_name=artifacts_container_name())


def _cache_job_root(job_id: str, root: str) -> str:
    normalized = root if root.endswith("/") else f"{root}/"
    with _JOB_ROOT_CACHE_LOCK:
        _JOB_ROOT_CACHE[job_id] = normalized
    return normalized


def list_blobs(prefix: str = "") -> list[str]:
    if not is_azure_artifacts_enabled():
        return []
    try:
        return _repo().list_blobs(prefix)
    except Exception as exc:
        logger.warning("[azure_artifacts] list_blobs failed: %s", exc)
        return []


def _root_from_course_slug(job_id: str, course_slug: str) -> str | None:
    """Try the known ``{course_slug}/{job_id}/`` layout (O(1) Azure check)."""
    slug = course_slug.strip().strip("/")
    if not slug:
        return None
    candidate = f"{slug}/{job_id}/"
    if _repo().prefix_exists(candidate):
        return _cache_job_root(job_id, candidate)
    return None


def _discover_job_root(job_id: str) -> str | None:
    """Locate the artifact root for *job_id* without scanning the whole container."""
    if not job_id:
        return None

    repo = _repo()

    # Legacy production layout: {job_id}/{file_name}/...
    legacy_prefix = f"{job_id}/"
    if repo.prefix_exists(legacy_prefix):
        return _cache_job_root(job_id, legacy_prefix)

    # Current layout: {course_slug}/{job_id}/...
    try:
        for slug in repo.list_prefixes():
            candidate = f"{slug}/{job_id}/"
            if repo.prefix_exists(candidate):
                return _cache_job_root(job_id, candidate)
    except Exception as exc:
        logger.warning("[azure_artifacts] prefix discovery failed for %s: %s", job_id, exc)

    return None


def get_job_artifact_root(
    job_id: str,
    *,
    course_slug: str | None = None,
) -> str | None:
    """Return cached or discovered artifact root prefix (ends with ``/``)."""
    if not job_id:
        return None
    with _JOB_ROOT_CACHE_LOCK:
        cached = _JOB_ROOT_CACHE.get(job_id)
    if cached:
        return cached
    if course_slug:
        root = _root_from_course_slug(job_id, course_slug)
        if root:
            return root
    return _discover_job_root(job_id)


def find_blobs_for_job(job_id: str, *, ends_with: str) -> list[str]:
    """Return blob paths in the artifacts container that belong to *job_id*."""
    if not job_id:
        return []

    root = get_job_artifact_root(job_id)
    if root:
        matches = [name for name in list_blobs(root) if name.endswith(ends_with)]
        if matches:
            return _rank_blob_matches(matches)

    # Last resort for unusual layouts - full container scan (slow).
    logger.debug("[azure_artifacts] falling back to full scan for job %s", job_id)
    matches = [
        name
        for name in list_blobs()
        if job_id in name and name.endswith(ends_with)
    ]
    ranked = _rank_blob_matches(matches)
    if ranked:
        first = ranked[0]
        if "/state/" in first:
            root = first.split("/state/", 1)[0] + "/"
        elif "/output/" in first:
            root = first.split("/output/", 1)[0] + "/"
        elif "/" in first:
            root = first.rsplit("/", 1)[0] + "/"
        else:
            root = ""
        if root:
            _cache_job_root(job_id, root)
    return ranked


def _rank_blob_matches(paths: list[str]) -> list[str]:
    """Prefer state/ over output/ over root-level paths."""

    def sort_key(path: str) -> tuple[int, int]:
        if "/state/" in path:
            tier = 0
        elif "/output/" in path:
            tier = 1
        else:
            tier = 2
        return tier, -len(path)

    return sorted(paths, key=sort_key)


def find_job_artifact_root(job_id: str) -> str | None:
    """Return the directory prefix for a job's artifacts (ends with ``/``)."""
    return get_job_artifact_root(job_id)


def download_json_blob(blob_path: str) -> dict[str, Any] | None:
    if not is_azure_artifacts_enabled():
        return None
    try:
        raw = _repo().download_text(blob_path.strip().lstrip("/"))
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("[azure_artifacts] download failed for %s: %s", blob_path, exc)
        return None


def _blob_path_candidates(root: str, filename: str) -> list[str]:
    root = root if root.endswith("/") else f"{root}/"
    return [
        f"{root}state/{filename}",
        f"{root}output/{filename}",
        f"{root}{filename}",
    ]


def load_json_for_job(
    job_id: str,
    filename: str,
    *,
    blob_root: str | None = None,
) -> dict[str, Any] | None:
    root = blob_root or get_job_artifact_root(job_id)
    if root:
        for blob_path in _blob_path_candidates(root, filename):
            data = download_json_blob(blob_path)
            if data is not None:
                return data
        matches = [
            name for name in list_blobs(root) if name.endswith(f"/{filename}") or name.endswith(filename)
        ]
        for blob_path in _rank_blob_matches(matches):
            data = download_json_blob(blob_path)
            if data is not None:
                return data

    for blob_path in find_blobs_for_job(job_id, ends_with=filename):
        data = download_json_blob(blob_path)
        if data is not None:
            return data
    return None


def load_shared_state_for_job(
    job_id: str,
    *,
    blob_root: str | None = None,
    course_slug: str | None = None,
) -> dict[str, Any] | None:
    root = blob_root or get_job_artifact_root(job_id, course_slug=course_slug)
    if root:
        for filename in _SHARED_STATE_FILENAMES:
            for blob_path in _blob_path_candidates(root, filename):
                data = download_json_blob(blob_path)
                if data is not None:
                    return data

    for filename in _SHARED_STATE_FILENAMES:
        data = load_json_for_job(job_id, filename, blob_root=blob_root)
        if data is not None:
            return data
    return None


def load_llm_outline_for_job(
    job_id: str,
    *,
    blob_root: str | None = None,
) -> dict[str, Any] | None:
    payload = load_json_for_job(job_id, "llm_to_outline.json", blob_root=blob_root)
    if not payload:
        return None
    inner = payload.get("llm_to_outline")
    return inner if isinstance(inner, dict) else payload


def list_json_artifacts_for_job(job_id: str) -> list[dict[str, Any]]:
    """Manifest of JSON blobs for a job (for GET /jobs/{id}/artifacts)."""
    items: list[dict[str, Any]] = []
    root = get_job_artifact_root(job_id)
    if root:
        blob_paths = [name for name in list_blobs(root) if name.endswith(".json")]
    else:
        blob_paths = find_blobs_for_job(job_id, ends_with=".json")
    for blob_path in blob_paths:
        name = blob_path.rsplit("/", 1)[-1]
        items.append({"name": name, "path": blob_path, "source": "azure"})
    return items


def find_job_id_for_course_slug(course_slug: str) -> str | None:
    """Return the most recently seen job id under a course slug prefix in Azure."""
    if not course_slug:
        return None
    slug = course_slug.strip().strip("/")
    job_ids: dict[str, str] = {}
    for blob in list_blobs(f"{slug}/"):
        parts = blob.split("/")
        for i, part in enumerate(parts):
            if part.startswith("j-") and len(part) > 10:
                job_ids[part] = blob
                break
            # dev layout: {slug}/{32-hex-job-id}/...
            if i >= 1 and len(part) == 32 and all(c in "0123456789abcdef" for c in part.lower()):
                job_ids[part] = blob
                break
    if not job_ids:
        # production layout may use different slug casing - scan all blobs
        slug_lower = slug.lower()
        for blob in list_blobs():
            if not blob.lower().startswith(slug_lower):
                continue
            for part in blob.split("/"):
                if part.startswith("j-") and len(part) > 10:
                    job_ids[part] = blob
    if not job_ids:
        return None
    # Last blob path lexicographically ~= most recent upload batch
    return max(job_ids.items(), key=lambda kv: kv[1])[0]

