import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from lectora_backend.repositories.blob_repository import BlobRepository

logger = logging.getLogger(__name__)


def download_input_file(adapter: Any, blob_path: str, destination: Path) -> Path:
    content = adapter._blob_repository.download_bytes(blob_path)
    destination.write_bytes(content)
    return destination


def upload_file(adapter: Any, local_path: Path, blob_path: str) -> str:
    content_type, _ = mimetypes.guess_type(str(local_path))
    adapter._blob_repository.upload_file(
        local_path=str(local_path),
        blob_path=blob_path,
        content_type=content_type,
    )
    return blob_path


def save_backend_state(
    adapter: Any,
    *,
    job_id: str,
    state_blob_path: str,
    state: dict[str, Any],
    artifact_refs: dict[str, Any],
) -> None:
    state["artifactRefs"] = artifact_refs
    adapter._state_manager.save(job_id, state, blob_path=state_blob_path)


def load_backend_state(adapter: Any, *, job_id: str, state_blob_path: str) -> dict[str, Any]:
    return adapter._state_manager.load(job_id, blob_path=state_blob_path)


def persist_a0_a1_outputs(
    adapter: Any,
    *,
    job_id: str,
    state_blob_path: str,
    a0_result: Any,
    a1_result: Any,
) -> dict[str, Any]:
    state = load_backend_state(adapter, job_id=job_id, state_blob_path=state_blob_path)
    blob_layout = state["blobLayout"]
    artifact_refs = state.setdefault("artifactRefs", {})
    local_doc_dir = Path(a0_result.shared_state_path).parent

    output_map = {
        "requestSpec": (
            Path(a0_result.output_files.request_spec),
            f"{blob_layout['output']}/request_spec.json",
        ),
        "provenanceLog": (
            Path(a0_result.output_files.provenance_log),
            f"{blob_layout['output']}/provenance_log.json",
        ),
        "llmToOutline": (
            Path(a0_result.output_files.llm_to_outline),
            f"{blob_layout['output']}/llm_to_outline.json",
        ),
        "pipelineSharedState": (
            Path(a0_result.output_files.shared_state),
            f"{blob_layout['state']}/pipeline_shared_state.json",
        ),
        "courseSpec": (
            local_doc_dir / "course_spec.json",
            f"{blob_layout['output']}/course_spec.json",
        ),
    }

    for key, (local_path, blob_path) in output_map.items():
        if local_path.exists():
            artifact_refs[key] = {"blobPath": upload_file(adapter, local_path, blob_path)}

    a1_status_path = local_doc_dir / "a1_status.json"
    a1_status_payload = (
        a1_result.model_dump(mode="json")
        if hasattr(a1_result, "model_dump")
        else {"status": str(getattr(a1_result, "status", "unknown"))}
    )
    a1_status_path.write_text(
        json.dumps(a1_status_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_refs["a1Status"] = {
        "blobPath": upload_file(
            adapter,
            a1_status_path,
            f"{blob_layout['logs']}/a1_status.json",
        )
    }

    for marker_name in ("a1_complete.json", "a1_failed.json", "a1_stopped.json"):
        marker_path = local_doc_dir / marker_name
        if marker_path.exists():
            artifact_refs["a1Marker"] = {
                "blobPath": upload_file(
                    adapter,
                    marker_path,
                    f"{blob_layout['logs']}/{marker_name}",
                )
            }
            break

    images_dir = local_doc_dir / "images"
    uploaded_images: list[dict[str, str]] = []
    if images_dir.exists():
        for image_path in sorted(images_dir.iterdir()):
            if image_path.is_file():
                blob_path = f"{blob_layout['images']}/{image_path.name}"
                uploaded_images.append(
                    {
                        "fileName": image_path.name,
                        "blobPath": upload_file(adapter, image_path, blob_path),
                    }
                )
    if uploaded_images:
        artifact_refs["images"] = uploaded_images

    save_backend_state(
        adapter,
        job_id=job_id,
        state_blob_path=state_blob_path,
        state=state,
        artifact_refs=artifact_refs,
    )
    return artifact_refs


def persist_s1_outputs(
    adapter: Any,
    *,
    job_id: str,
    state_blob_path: str,
    pipeline_shared_state_path: str,
    s1_result: Any,
) -> dict[str, Any]:
    state = load_backend_state(adapter, job_id=job_id, state_blob_path=state_blob_path)
    blob_layout = state["blobLayout"]
    artifact_refs = state.setdefault("artifactRefs", {})
    local_doc_dir = Path(pipeline_shared_state_path).parent

    report_path = (
        Path(s1_result.report_path) if s1_result.report_path else local_doc_dir / "s1_validation.json"
    )
    if report_path.exists():
        artifact_refs["s1Validation"] = {
            "blobPath": upload_file(
                adapter,
                report_path,
                f"{blob_layout['output']}/s1_validation.json",
            )
        }

    shared_state_path = Path(pipeline_shared_state_path)
    if shared_state_path.exists():
        artifact_refs["pipelineSharedState"] = {
            "blobPath": upload_file(
                adapter,
                shared_state_path,
                f"{blob_layout['state']}/pipeline_shared_state.json",
            )
        }

    save_backend_state(
        adapter,
        job_id=job_id,
        state_blob_path=state_blob_path,
        state=state,
        artifact_refs=artifact_refs,
    )
    return artifact_refs


def persist_a2_outputs(
    adapter: Any,
    *,
    job_id: str,
    state_blob_path: str,
    pipeline_shared_state_path: str,
    enriched_sections_path: Path,
    a2_result: Any,
    final_docx_path: str | None = None,
) -> dict[str, Any]:
    state = load_backend_state(adapter, job_id=job_id, state_blob_path=state_blob_path)
    blob_layout = state["blobLayout"]
    artifact_refs = state.setdefault("artifactRefs", {})

    kc_plan_path = Path(pipeline_shared_state_path).parent / "kc_plan.json"

    docx_local = (
        Path(final_docx_path)
        if final_docx_path
        else (Path(a2_result.study_guide_docx) if a2_result.study_guide_docx else None)
    )

    output_map = {
        "sectionMap": (
            enriched_sections_path,
            f"{blob_layout['output']}/enriched_sections.json",
        ),
        "kcPlan": (
            kc_plan_path,
            f"{blob_layout['output']}/kc_plan.json",
        ),
        "generatedContent": (
            Path(a2_result.generated_content_json),
            f"{blob_layout['output']}/generated_content.json",
        ),
        "pipelineSharedState": (
            Path(pipeline_shared_state_path),
            f"{blob_layout['state']}/pipeline_shared_state.json",
        ),
    }
    if docx_local:
        output_map["generatedStudyGuide"] = (
            docx_local,
            f"{blob_layout['output']}/study_guide.docx",
        )

    for key, (local_path, blob_path) in output_map.items():
        if local_path.exists():
            artifact_refs[key] = {"blobPath": upload_file(adapter, local_path, blob_path)}
            logger.info("Uploaded artifact %s → %s", key, blob_path)
            if key == "generatedStudyGuide":
                from lectora_backend.config import settings

                generated_repo = BlobRepository(
                    container_name=settings.generated_courses_container_name,
                )
                content_type, _ = mimetypes.guess_type(str(local_path))
                generated_repo.upload_file(
                    local_path=str(local_path),
                    blob_path=blob_path,
                    content_type=content_type,
                )
                logger.info(
                    "Uploaded generatedStudyGuide → %s/%s",
                    settings.generated_courses_container_name,
                    blob_path,
                )
        else:
            logger.warning("Artifact %s not found at %s — skipping", key, local_path)

    save_backend_state(
        adapter,
        job_id=job_id,
        state_blob_path=state_blob_path,
        state=state,
        artifact_refs=artifact_refs,
    )
    return artifact_refs


def persist_s2_outputs(
    adapter: Any,
    *,
    job_id: str,
    state_blob_path: str,
    pipeline_shared_state_path: str,
    s2_result: Any,
) -> dict[str, Any]:
    state = load_backend_state(adapter, job_id=job_id, state_blob_path=state_blob_path)
    blob_layout = state["blobLayout"]
    artifact_refs = state.setdefault("artifactRefs", {})

    report_path = (
        Path(s2_result.report_path)
        if s2_result.report_path
        else Path(pipeline_shared_state_path).parent / "s2_validation.json"
    )
    if report_path.exists():
        artifact_refs["s2Validation"] = {
            "blobPath": upload_file(
                adapter,
                report_path,
                f"{blob_layout['output']}/s2_validation.json",
            )
        }

    shared_state_path = Path(pipeline_shared_state_path)
    if shared_state_path.exists():
        artifact_refs["pipelineSharedState"] = {
            "blobPath": upload_file(
                adapter,
                shared_state_path,
                f"{blob_layout['state']}/pipeline_shared_state.json",
            )
        }

    save_backend_state(
        adapter,
        job_id=job_id,
        state_blob_path=state_blob_path,
        state=state,
        artifact_refs=artifact_refs,
    )
    return artifact_refs

