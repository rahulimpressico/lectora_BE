from __future__ import annotations


def local_artifact_path_candidates(relative_path: str) -> list[str]:
    """
    Return candidate relative paths for local artifact lookup.

    Supports both modern and legacy layouts:
    - Modern: `pipeline/courses/{course_slug}/{job_id}/...`
    - Legacy: `pipeline/shared_state/{run_or_slug}/...`
    """
    path = (relative_path or "").strip().lstrip("/")
    if not path:
        return []

    candidates = [path]

    if path.startswith("courses/"):
        candidates.append(path[len("courses/"):])
    elif not path.startswith("shared_state/"):
        candidates.append(f"shared_state/{path}")

    if "/shared_state/" in path:
        suffix = path.split("/shared_state/", 1)[1]
        if suffix:
            candidates.append(suffix)

    seen: set[str] = set()
    deduped: list[str] = []
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        deduped.append(c)
    return deduped

