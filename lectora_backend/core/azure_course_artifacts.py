"""
Read / resolve pipeline artifacts from the Azure ``course-generation-artifacts`` container.

When Azure is configured, job APIs and the Asset Library prefer this container
over local ``pipeline/courses`` / ``pipeline/shared_state`` files.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from lectora_backend.repositories.blob_repository import BlobRepository

logger = logging.getLogger(__name__)

_SHARED_STATE_FILENAMES = (
    "pipeline_shared_state.json",
    "shared_state.json",
)


def is_azure_artifacts_enabled() -> bool:
    from lectora_backend.config import settings

    return settings.is_azure_storage_configured()


def artifacts_container_name() -> str:
    from lectora_backend.config import settings

    return settings.course_generation_artifacts_container_name


def _repo() -> BlobRepository:
    return BlobRepository(container_name=artifacts_container_name())


def list_blobs(prefix: str = "") -> list[str]:
    if not is_azure_artifacts_enabled():
        return []
    try:
        return _repo().list_blobs(prefix)
    except Exception as exc:
        logger.warning("[azure_artifacts] list_blobs failed: %s", exc)
        return []


def find_blobs_for_job(job_id: str, *, ends_with: str) -> list[str]:
    """Return blob paths in the artifacts container that belong to *job_id*."""
    if not job_id:
        return []
    matches = [
        name
        for name in list_blobs()
        if job_id in name and name.endswith(ends_with)
    ]
    return _rank_blob_matches(matches)


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
    for blob in find_blobs_for_job(job_id, ends_with=".json"):
        if "/state/" in blob:
            return blob.split("/state/", 1)[0] + "/"
        if "/output/" in blob:
            return blob.split("/output/", 1)[0] + "/"
        if "/" in blob:
            return blob.rsplit("/", 1)[0] + "/"
    return None


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


def load_json_for_job(job_id: str, filename: str) -> dict[str, Any] | None:
    for blob_path in find_blobs_for_job(job_id, ends_with=filename):
        data = download_json_blob(blob_path)
        if data is not None:
            return data
    return None


def load_shared_state_for_job(job_id: str) -> dict[str, Any] | None:
    for filename in _SHARED_STATE_FILENAMES:
        data = load_json_for_job(job_id, filename)
        if data is not None:
            return data
    return None


def load_llm_outline_for_job(job_id: str) -> dict[str, Any] | None:
    payload = load_json_for_job(job_id, "llm_to_outline.json")
    if not payload:
        return None
    inner = payload.get("llm_to_outline")
    return inner if isinstance(inner, dict) else payload


def list_json_artifacts_for_job(job_id: str) -> list[dict[str, Any]]:
    """Manifest of JSON blobs for a job (for GET /jobs/{id}/artifacts)."""
    items: list[dict[str, Any]] = []
    for blob_path in find_blobs_for_job(job_id, ends_with=".json"):
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
        # production layout may use different slug casing — scan all blobs
        slug_lower = slug.lower()
        for blob in list_blobs():
            if not blob.lower().startswith(slug_lower):
                continue
            for part in blob.split("/"):
                if part.startswith("j-") and len(part) > 10:
                    job_ids[part] = blob
    if not job_ids:
        return None
    # Last blob path lexicographically ≈ most recent upload batch
    return max(job_ids.items(), key=lambda kv: kv[1])[0]
