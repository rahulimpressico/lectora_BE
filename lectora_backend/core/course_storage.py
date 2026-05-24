"""Course-title-based Azure/local storage paths (replaces job-id roots)."""
from __future__ import annotations

import re


def sanitize_course_slug(value: str) -> str:
    """Normalize a course title or topic for safe blob path segments."""
    normalized = re.sub(r"\s+", "_", (value or "").strip())
    normalized = re.sub(r"[^A-Za-z0-9._-]", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("._")
    if len(normalized) < 2:
        return "course"
    return normalized[:120]


def course_folder_from_blob_path(blob_path: str) -> str | None:
    """First path segment under uploaded-documents (course topic folder)."""
    clean = blob_path.strip().lstrip("/")
    for prefix in ("uploaded-documents/", "uploads/"):
        if clean.startswith(prefix):
            clean = clean[len(prefix) :]
    parts = [p for p in clean.split("/") if p]
    return parts[0] if parts else None


def course_title_from_folder(folder: str) -> str:
    """Human-readable title from a sanitized folder slug."""
    return folder.replace("_", " ").strip()


def strip_legacy_outputs_prefix(path: str) -> str:
    """Normalize artifact paths that still use the old ``outputs/`` segment."""
    clean = path.strip().lstrip("/")
    if clean == "outputs":
        return ""
    if clean.startswith("outputs/"):
        return clean[len("outputs/") :]
    return clean


def course_output_root(course_title: str) -> str:
    """Azure/local root for one course: ``{slug}/`` at container root."""
    return sanitize_course_slug(course_title)


def resolve_course_title(
    *,
    explicit_title: str | None = None,
    blob_path: str | None = None,
    fallback: str = "course",
) -> str:
    """Pick the best course title for storage layout."""
    if explicit_title and explicit_title.strip():
        return explicit_title.strip()
    if blob_path:
        folder = course_folder_from_blob_path(blob_path)
        if folder:
            return course_title_from_folder(folder)
    return fallback


def storage_path_matches(prefixes: set[str], path: str) -> bool:
    """True when *path* is under any normalized prefix (file or folder)."""
    clean = path.strip().lstrip("/")
    if not clean:
        return False
    for raw in prefixes:
        p = raw.strip().lstrip("/").rstrip("/")
        if not p:
            continue
        if clean == p or clean.startswith(f"{p}/"):
            return True
    return False
