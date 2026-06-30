"""Adapter between blob-backed job state and the file-based pipeline agents."""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from lectora_backend.core.blob_layout import build_blob_layout_for_course
from lectora_backend.core.pipeline.pipeline_adapter_a2_flow import run_a2_flow
from lectora_backend.core.pipeline.pipeline_adapter_artifacts import (
    download_input_file,
    load_backend_state,
    persist_a0_a1_outputs,
    persist_a2_outputs,
    persist_s1_outputs,
    persist_s2_outputs,
    save_backend_state,
    upload_file,
)
from lectora_backend.core.state_manager import StateManager
from lectora_backend.pipeline.agent.a0_request_synthesizer.main import A0RequestSynthesizer
from lectora_backend.pipeline.agent.a1_outline_interpreter.main import run as a1_run
from lectora_backend.pipeline.agent.s1_validator.main import S1Validator
from lectora_backend.pipeline.models.validation import S1Status
from lectora_backend.pipeline.shared_utils.validation_helpers import llm_outline_from_to_data
from lectora_backend.repositories.blob_repository import BlobRepository

logger = logging.getLogger(__name__)

_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class PipelineAdapter:
    def __init__(
        self,
        blob_repository: BlobRepository | None = None,
        state_manager: StateManager | None = None,
    ) -> None:
        self._blob_repository = blob_repository or BlobRepository()
        self._state_manager = state_manager or StateManager(self._blob_repository)

    def _require_blob_path(self, manifest_entry: dict | None, field_name: str) -> str:
        if not manifest_entry or not manifest_entry.get("blobPath"):
            raise ValueError(f"Missing required input blob path for {field_name}.")
        return str(manifest_entry["blobPath"])

    def _download_input_file(self, blob_path: str, destination: Path) -> Path:
        return download_input_file(self, blob_path, destination)

    def _upload_file(self, local_path: Path, blob_path: str) -> str:
        return upload_file(self, local_path, blob_path)

    def _save_backend_state(
        self,
        *,
        job_id: str,
        state_blob_path: str,
        state: dict[str, Any],
        artifact_refs: dict[str, Any],
    ) -> None:
        save_backend_state(
            self,
            job_id=job_id,
            state_blob_path=state_blob_path,
            state=state,
            artifact_refs=artifact_refs,
        )

    def _load_backend_state(self, *, job_id: str, state_blob_path: str) -> dict[str, Any]:
        return load_backend_state(self, job_id=job_id, state_blob_path=state_blob_path)

    def _normalize_input_docs(
        self,
        state: dict[str, Any],
        state_blob_path: str,
        study_guide_blob: str,
        timed_outline_blob: str,
    ) -> tuple[str, str]:
        blob_layout = state.get("blobLayout")
        if blob_layout is None:
            course_title = (state.get("request") or {}).get("courseTitle") or ""
            layout = build_blob_layout_for_course(course_title)
            blob_layout = layout.to_dict()
            state["blobLayout"] = blob_layout

        sg_filename = PurePosixPath(study_guide_blob).name
        to_filename = PurePosixPath(timed_outline_blob).name
        normalized_study_guide = f"{blob_layout['doc']}/{sg_filename}"
        normalized_timed_outline = f"{blob_layout['doc']}/{to_filename}"

        def _mime_for_blob(blob_path: str) -> str:
            return "application/pdf" if blob_path.lower().endswith(".pdf") else _MIME_DOCX

        if study_guide_blob != normalized_study_guide:
            self._blob_repository.upload_bytes(
                blob_path=normalized_study_guide,
                content=self._blob_repository.download_bytes(study_guide_blob),
                content_type=_mime_for_blob(normalized_study_guide),
            )
            state["inputManifest"]["studyGuide"]["blobPath"] = normalized_study_guide

        if timed_outline_blob != normalized_timed_outline:
            self._blob_repository.upload_bytes(
                blob_path=normalized_timed_outline,
                content=self._blob_repository.download_bytes(timed_outline_blob),
                content_type=_mime_for_blob(normalized_timed_outline),
            )
            state["inputManifest"]["timedOutline"]["blobPath"] = normalized_timed_outline

        self._state_manager.save(
            state["run"]["jobId"],
            state,
            blob_path=state_blob_path,
        )
        return normalized_study_guide, normalized_timed_outline

    def prepare_inputs(
        self,
        job_id: str,
        *,
        state_blob_path: str,
    ) -> dict[str, object]:
        state = self._state_manager.load(job_id, blob_path=state_blob_path)
        manifest = state["inputManifest"]

        study_guide_blob = self._require_blob_path(manifest.get("studyGuide"), "studyGuide")

        timed_outline_entry = manifest.get("timedOutline")
        timed_outline_blob: str | None = (
            timed_outline_entry.get("blobPath") if timed_outline_entry else None
        )

        temp_dir = Path(tempfile.mkdtemp(prefix=f"{job_id}_pipeline_"))
        study_guide_path = self._download_input_file(
            study_guide_blob,
            temp_dir / PurePosixPath(study_guide_blob).name,
        )

        timed_outline_path: Path | None = None
        if timed_outline_blob:
            study_guide_blob, timed_outline_blob = self._normalize_input_docs(
                state,
                state_blob_path,
                study_guide_blob,
                timed_outline_blob,
            )
            timed_outline_path = self._download_input_file(
                timed_outline_blob,
                temp_dir / PurePosixPath(timed_outline_blob).name,
            )
        else:
            blob_layout = state.get("blobLayout")
            if blob_layout is None:
                course_title = (state.get("request") or {}).get("courseTitle") or ""
                layout = build_blob_layout_for_course(course_title)
                state["blobLayout"] = layout.to_dict()
                self._state_manager.save(state["run"]["jobId"], state, blob_path=state_blob_path)

        return {
            "state": state,
            "tempDir": temp_dir,
            "studyGuidePath": study_guide_path,
            "timedOutlinePath": timed_outline_path,
        }

    def _persist_a0_a1_outputs(
        self,
        *,
        job_id: str,
        state_blob_path: str,
        a0_result: Any,
        a1_result: Any,
    ) -> dict[str, Any]:
        return persist_a0_a1_outputs(
            self,
            job_id=job_id,
            state_blob_path=state_blob_path,
            a0_result=a0_result,
            a1_result=a1_result,
        )

    def _persist_s1_outputs(
        self,
        *,
        job_id: str,
        state_blob_path: str,
        pipeline_shared_state_path: str,
        s1_result: Any,
    ) -> dict[str, Any]:
        return persist_s1_outputs(
            self,
            job_id=job_id,
            state_blob_path=state_blob_path,
            pipeline_shared_state_path=pipeline_shared_state_path,
            s1_result=s1_result,
        )

    def _persist_a2_outputs(
        self,
        *,
        job_id: str,
        state_blob_path: str,
        pipeline_shared_state_path: str,
        enriched_sections_path: Path,
        a2_result: Any,
        final_docx_path: str | None = None,
    ) -> dict[str, Any]:
        return persist_a2_outputs(
            self,
            job_id=job_id,
            state_blob_path=state_blob_path,
            pipeline_shared_state_path=pipeline_shared_state_path,
            enriched_sections_path=enriched_sections_path,
            a2_result=a2_result,
            final_docx_path=final_docx_path,
        )

    def _persist_s2_outputs(
        self,
        *,
        job_id: str,
        state_blob_path: str,
        pipeline_shared_state_path: str,
        s2_result: Any,
    ) -> dict[str, Any]:
        return persist_s2_outputs(
            self,
            job_id=job_id,
            state_blob_path=state_blob_path,
            pipeline_shared_state_path=pipeline_shared_state_path,
            s2_result=s2_result,
        )

    @staticmethod
    def _llm_outline_from_to_data(to_data: dict[str, Any]) -> dict[str, Any]:
        return llm_outline_from_to_data(to_data)

    def _write_to_override_json(
        self,
        to_override: dict[str, Any],
        temp_dir: Path,
    ) -> Path:
        to_json_path = temp_dir / "user_edited_to.json"
        payload = {"llm_to_outline": self._llm_outline_from_to_data(to_override)}
        to_json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("[pipeline_adapter] Wrote user-edited TO override → %s", to_json_path)
        return to_json_path

    def run_a0(
        self,
        job_id: str,
        *,
        state_blob_path: str,
        prepared_inputs: dict[str, object] | None = None,
        gate_attempt: int = 1,
    ) -> dict[str, Any]:
        inputs = prepared_inputs or self.prepare_inputs(job_id, state_blob_path=state_blob_path)
        temp_dir = Path(inputs["tempDir"])
        study_guide_path = Path(inputs["studyGuidePath"])
        raw_to_path = inputs.get("timedOutlinePath")
        timed_outline_path: Path | None = Path(raw_to_path) if raw_to_path else None

        backend_state = self._load_backend_state(job_id=job_id, state_blob_path=state_blob_path)
        course_difficulty = str(
            backend_state.get("courseDifficulty")
            or backend_state.get("course_difficulty")
            or "intermediate"
        ).strip().lower()

        effective_to_path: str | None = str(timed_outline_path) if timed_outline_path else None
        to_override = backend_state.get("toOverride")
        if to_override and isinstance(to_override, dict):
            override_json = self._write_to_override_json(to_override, temp_dir)
            effective_to_path = str(override_json)

        _sg_ext = study_guide_path.suffix.lower()
        _a0_docx_paths: list[str] = [] if _sg_ext == ".pdf" else [str(study_guide_path)]
        _a0_pdf_paths: list[str] = [str(study_guide_path)] if _sg_ext == ".pdf" else []

        a0_result = A0RequestSynthesizer(
            docx_paths=_a0_docx_paths,
            pdf_paths=_a0_pdf_paths,
            to_outline_doc_path=effective_to_path,
            output_dir=str(temp_dir),
            course_difficulty=course_difficulty,
        ).run()

        return {
            "tempDir": str(temp_dir),
            "studyGuidePath": str(study_guide_path),
            "timedOutlinePath": str(timed_outline_path) if timed_outline_path else None,
            "a0": a0_result,
            "a0SharedStatePath": a0_result.shared_state_path,
            "courseDifficulty": course_difficulty,
        }

    def run_a1(
        self,
        job_id: str,
        *,
        state_blob_path: str,
        a0_result: Any,
        study_guide_path: str,
        s1_retry_feedback: dict[str, Any] | None = None,
        gate_attempt: int = 1,
    ) -> dict[str, Any]:
        a1_feedback = dict(s1_retry_feedback) if s1_retry_feedback else None
        if a1_feedback is not None:
            a1_feedback.setdefault("gateAttempt", gate_attempt)

        a1_result = a1_run(
            shared_state_path=a0_result.shared_state_path,
            docx_path=study_guide_path,
            feedback=a1_feedback,
        )

        artifact_refs = self._persist_a0_a1_outputs(
            job_id=job_id,
            state_blob_path=state_blob_path,
            a0_result=a0_result,
            a1_result=a1_result,
        )

        return {
            "a1": a1_result,
            "artifactRefs": artifact_refs,
        }

    def run_a0_a1(
        self,
        job_id: str,
        *,
        state_blob_path: str,
        prepared_inputs: dict[str, object] | None = None,
        s1_retry_feedback: dict[str, Any] | None = None,
        gate_attempt: int = 1,
    ) -> dict[str, Any]:
        a0_ctx = self.run_a0(
            job_id,
            state_blob_path=state_blob_path,
            prepared_inputs=prepared_inputs,
            gate_attempt=gate_attempt,
        )
        a1_ctx = self.run_a1(
            job_id,
            state_blob_path=state_blob_path,
            a0_result=a0_ctx["a0"],
            study_guide_path=a0_ctx["studyGuidePath"],
            s1_retry_feedback=s1_retry_feedback,
            gate_attempt=gate_attempt,
        )
        return {
            "tempDir": a0_ctx["tempDir"],
            "studyGuidePath": a0_ctx["studyGuidePath"],
            "timedOutlinePath": a0_ctx["timedOutlinePath"],
            "a0": a0_ctx["a0"],
            "a1": a1_ctx["a1"],
            "a0SharedStatePath": a0_ctx["a0SharedStatePath"],
            "artifactRefs": a1_ctx["artifactRefs"],
        }

    def run_s1(
        self,
        job_id: str,
        *,
        state_blob_path: str,
        pipeline_shared_state_path: str,
    ) -> dict[str, Any]:
        s1_result = S1Validator(pipeline_shared_state_path).run()
        artifact_refs = self._persist_s1_outputs(
            job_id=job_id,
            state_blob_path=state_blob_path,
            pipeline_shared_state_path=pipeline_shared_state_path,
            s1_result=s1_result,
        )
        return {
            "s1": s1_result,
            "artifactRefs": artifact_refs,
        }

    def run_a2(
        self,
        job_id: str,
        *,
        state_blob_path: str,
        pipeline_shared_state_path: str,
        study_guide_path: str,
        course_difficulty: str = "intermediate",
        extra_source_blob_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        return run_a2_flow(
            self,
            job_id,
            state_blob_path=state_blob_path,
            pipeline_shared_state_path=pipeline_shared_state_path,
            study_guide_path=study_guide_path,
            course_difficulty=course_difficulty,
            extra_source_blob_paths=extra_source_blob_paths,
        )

    @staticmethod
    def s1_status_blocks_pipeline(status: S1Status) -> bool:
        return status in {S1Status.blocked, S1Status.blocker}

    @staticmethod
    def build_s1_outcome(s1_result: Any) -> "ValidationOutcome":
        from lectora_backend.models.job_enums import ValidationOutcome

        if getattr(s1_result.status, "value", "") == "pass_with_warnings":
            return ValidationOutcome.WARNING
        return ValidationOutcome.PASS

    @staticmethod
    def build_s1_retry_feedback(s1_result: Any) -> dict[str, Any]:
        issues = [
            {
                "message": getattr(issue, "message", str(issue)),
                "severity": getattr(issue, "severity", None),
                "path": getattr(issue, "path", None),
            }
            for issue in getattr(s1_result, "issues", []) or []
        ]
        return {
            "source": "S1",
            "status": getattr(s1_result.status, "value", str(s1_result.status)),
            "issues": issues,
        }

    @staticmethod
    def build_s1_error_detail(s1_result: Any) -> str:
        first_issue = (
            s1_result.issues[0].message
            if getattr(s1_result, "issues", None)
            else "S1 validation blocked A2."
        )
        payload = {
            "code": "S1_VALIDATION_BLOCKED",
            "message": first_issue,
            "stage": "S1",
            "retryable": False,
            "validationStatus": getattr(s1_result.status, "value", str(s1_result.status)),
        }
        return json.dumps(payload)

