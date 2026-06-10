"""
Upload local pipeline course artifacts to Azure Blob Storage.

Mirrors the blob layout used by PipelineAdapter in production so dev-mode runs
(pipeline/courses/{slug}/{job_id}/) land in course-generation-artifacts under:

  {course_slug}/{job_id}/output/*.json
  {course_slug}/{job_id}/state/pipeline_shared_state.json
  {course_slug}/{job_id}/images/*
  {course_slug}/{job_id}/doc/*
  {course_slug}/{job_id}/logs/*
"""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from lectora_backend.core.blob_layout import build_blob_layout_for_course
from lectora_backend.repositories.blob_repository import BlobRepository

logger = logging.getLogger(__name__)

# Local filename → (blob subdir, blob filename override or None = same name)
_ROOT_OUTPUT_FILES: dict[str, tuple[str, str | None]] = {
    "request_spec.json": ("output", None),
    "provenance_log.json": ("output", None),
    "llm_to_outline.json": ("output", None),
    "course_spec.json": ("output", None),
    "enriched_sections.json": ("output", None),
    "kc_plan.json": ("output", None),
    "generated_content.json": ("output", None),
    "s1_validation.json": ("output", None),
    "s2_validation.json": ("output", None),
    "shared_state.json": ("state", "pipeline_shared_state.json"),
}

_LOG_FILES = (
    "a1_status.json",
    "a1_complete.json",
    "a1_failed.json",
    "a1_stopped.json",
)


def sync_local_artifacts_to_azure(
    *,
    job_id: str,
    course_title: str,
    shared_state_path: str,
    study_guide_path: str | None = None,
) -> dict[str, Any]:
    """
    Upload all pipeline artifacts from a local course run to Azure Blob Storage.

    No-op when Azure is not configured.  Never raises — errors are collected in
    the returned dict so a failed upload does not fail the pipeline job.
    """
    from lectora_backend.config import settings

    if not settings.is_azure_storage_configured():
        logger.debug("[artifact_sync] Azure not configured — skipping upload")
        return {"uploaded": 0, "skipped": True, "reason": "azure_not_configured"}

    local_dir = Path(shared_state_path).resolve().parent
    if not local_dir.is_dir():
        return {"uploaded": 0, "skipped": True, "reason": "local_dir_missing"}

    layout = build_blob_layout_for_course(course_title, job_id=job_id)
    blob_dirs = layout.to_dict()
    container = settings.course_generation_artifacts_container_name
    repo = BlobRepository(container_name=container)

    uploaded = 0
    errors: list[str] = []

    def _upload(local_path: Path, blob_path: str) -> None:
        nonlocal uploaded
        try:
            content_type, _ = mimetypes.guess_type(str(local_path))
            repo.upload_file(
                local_path=str(local_path),
                blob_path=blob_path,
                content_type=content_type,
            )
            uploaded += 1
            logger.info("[artifact_sync] %s → %s/%s", local_path.name, container, blob_path)
        except Exception as exc:
            msg = f"{local_path.name}: {exc}"
            errors.append(msg)
            logger.warning("[artifact_sync] Upload failed: %s", msg)

    # ── Root-level JSON / DOCX files ──────────────────────────────────────────
    for filename, (subdir_key, blob_name) in _ROOT_OUTPUT_FILES.items():
        local_path = local_dir / filename
        if not local_path.is_file():
            continue
        target_name = blob_name or filename
        blob_path = f"{blob_dirs[subdir_key]}/{target_name}"
        _upload(local_path, blob_path)

    # ── Final DOCX → generated-courses container (Generated Courses card) ─────
    generated_repo = BlobRepository(
        container_name=settings.generated_courses_container_name,
    )
    sg_candidates = [
        Path(study_guide_path).resolve() if study_guide_path else None,
        (local_dir / "study_guide.docx").resolve(),
    ]
    for sg in sg_candidates:
        if sg and sg.is_file():
            blob_path = f"{blob_dirs['output']}/study_guide.docx"
            try:
                content_type, _ = mimetypes.guess_type(str(sg))
                generated_repo.upload_file(
                    local_path=str(sg),
                    blob_path=blob_path,
                    content_type=content_type,
                )
                uploaded += 1
                logger.info(
                    "[artifact_sync] %s → %s/%s",
                    sg.name,
                    settings.generated_courses_container_name,
                    blob_path,
                )
            except Exception as exc:
                errors.append(f"{sg.name} (generated-courses): {exc}")
            break

    # ── Pipeline logs → regedlectoraaistorage (pipeline-artifacts card) ───────
    pipeline_repo = BlobRepository(container_name=settings.blob_container_name)

    def _upload_pipeline_log(local_path: Path, blob_path: str) -> None:
        nonlocal uploaded
        try:
            content_type, _ = mimetypes.guess_type(str(local_path))
            pipeline_repo.upload_file(
                local_path=str(local_path),
                blob_path=blob_path,
                content_type=content_type,
            )
            uploaded += 1
        except Exception as exc:
            errors.append(f"{local_path.name} (pipeline): {exc}")

    for log_name in _LOG_FILES:
        log_path = local_dir / log_name
        if log_path.is_file():
            _upload_pipeline_log(log_path, f"{blob_dirs['logs']}/{log_name}")

    logs_subdir = local_dir / "logs"
    if logs_subdir.is_dir():
        for log_path in sorted(logs_subdir.glob("*.json")):
            _upload_pipeline_log(log_path, f"{blob_dirs['logs']}/{log_path.name}")

    # ── LLM trace JSONL → regedlectoraaistorage (costing dashboard) ───────────
    traces_root = Path(__file__).resolve().parent.parent / "pipeline" / "logs"
    if traces_root.is_dir():
        for trace_path in sorted(traces_root.rglob("llm_traces.jsonl")):
            rel = trace_path.relative_to(traces_root).as_posix()
            _upload_pipeline_log(trace_path, f"{blob_dirs['logs']}/llm_traces/{rel}")

    # ── images/ and doc/ subdirectories ─────────────────────────────────────
    for subdir_key in ("images", "doc"):
        subdir = local_dir / subdir_key
        if not subdir.is_dir():
            continue
        for file_path in sorted(subdir.rglob("*")):
            if file_path.is_file():
                rel = file_path.relative_to(subdir).as_posix()
                _upload(file_path, f"{blob_dirs[subdir_key]}/{rel}")

    return {
        "uploaded": uploaded,
        "container": container,
        "generatedCoursesContainer": settings.generated_courses_container_name,
        "blobRoot": layout.root,
        "errors": errors,
        "skipped": False,
    }
