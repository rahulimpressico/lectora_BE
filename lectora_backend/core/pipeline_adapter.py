"""Adapter between Blob-backed job state and Rahul's file-based pipeline."""
from __future__ import annotations

import json
import logging
import mimetypes
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

from lectora_backend.core.blob_layout import build_blob_layout_from_input_blob
from lectora_backend.core.state_manager import StateManager
from lectora_backend.pipeline.agent.a0_request_synthesizer.main import A0RequestSynthesizer
from lectora_backend.pipeline.agent.a1_outline_interpreter.main import run as a1_run
from lectora_backend.pipeline.agent.a2_content_generator.main import (
    A2ContentGenerator,
    render_study_guide_from_state,
)
from lectora_backend.pipeline.agent.s1_validator.main import S1Validator
from lectora_backend.pipeline.agent.s2_validator.main import S2Validator
from lectora_backend.pipeline.agent.section_mapper.main import run as section_mapper_run
from lectora_backend.pipeline.agent.kc_planner.main import run as kc_planner_run
from lectora_backend.pipeline.models.validation import S1Status
from lectora_backend.models.constants import MAX_A2_S2_CYCLES
from lectora_backend.repositories.blob_repository import BlobRepository


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
            raise ValueError(
                f"Missing required input blob path for {field_name}.")
        return str(manifest_entry["blobPath"])

    def _download_input_file(self, blob_path: str, destination: Path) -> Path:
        content = self._blob_repository.download_bytes(blob_path)
        destination.write_bytes(content)
        return destination

    def _upload_file(self, local_path: Path, blob_path: str) -> str:
        content_type, _ = mimetypes.guess_type(str(local_path))
        self._blob_repository.upload_file(
            local_path=str(local_path),
            blob_path=blob_path,
            content_type=content_type,
        )
        return blob_path

    def _normalize_input_docs(
        self,
        state: dict[str, Any],
        state_blob_path: str,
        study_guide_blob: str,
        timed_outline_blob: str,
    ) -> tuple[str, str]:
        blob_layout = state.get("blobLayout")
        if blob_layout is None:
            layout = build_blob_layout_from_input_blob(
                study_guide_blob,
                state["run"]["jobId"],
            )
            blob_layout = layout.to_dict()
            state["blobLayout"] = blob_layout

        sg_filename = PurePosixPath(study_guide_blob).name
        to_filename = PurePosixPath(timed_outline_blob).name
        normalized_study_guide = f"{blob_layout['doc']}/{sg_filename}"
        normalized_timed_outline = f"{blob_layout['doc']}/{to_filename}"

        if study_guide_blob != normalized_study_guide:
            self._blob_repository.upload_bytes(
                blob_path=normalized_study_guide,
                content=self._blob_repository.download_bytes(study_guide_blob),
                content_type=_MIME_DOCX,
            )
            state["inputManifest"]["studyGuide"]["blobPath"] = normalized_study_guide

        if timed_outline_blob != normalized_timed_outline:
            self._blob_repository.upload_bytes(
                blob_path=normalized_timed_outline,
                content=self._blob_repository.download_bytes(timed_outline_blob),
                content_type=_MIME_DOCX,
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

        study_guide_blob = self._require_blob_path(
            manifest.get("studyGuide"), "studyGuide")

        # timedOutline is optional — may not be provided when the user relies on
        # an auto-generated or user-edited TO passed via toOverride instead.
        timed_outline_entry = manifest.get("timedOutline")
        timed_outline_blob: str | None = (
            timed_outline_entry.get("blobPath")
            if timed_outline_entry
            else None
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
            # No timedOutline blob — normalize study guide alone (updates blobLayout)
            blob_layout = state.get("blobLayout")
            if blob_layout is None:
                from lectora_backend.core.blob_layout import build_blob_layout_from_input_blob
                layout = build_blob_layout_from_input_blob(
                    study_guide_blob, state["run"]["jobId"])
                state["blobLayout"] = layout.to_dict()
                self._state_manager.save(
                    state["run"]["jobId"], state, blob_path=state_blob_path)

        return {
            "state": state,
            "tempDir": temp_dir,
            "studyGuidePath": study_guide_path,
            "timedOutlinePath": timed_outline_path,
        }

    def _save_backend_state(
        self,
        *,
        job_id: str,
        state_blob_path: str,
        state: dict[str, Any],
        artifact_refs: dict[str, Any],
    ) -> None:
        state["artifactRefs"] = artifact_refs
        self._state_manager.save(job_id, state, blob_path=state_blob_path)

    def _load_backend_state(self, *, job_id: str, state_blob_path: str) -> dict[str, Any]:
        return self._state_manager.load(job_id, blob_path=state_blob_path)

    def _persist_a0_a1_outputs(
        self,
        *,
        job_id: str,
        state_blob_path: str,
        a0_result: Any,
        a1_result: Any,
    ) -> dict[str, Any]:
        state = self._load_backend_state(
            job_id=job_id, state_blob_path=state_blob_path)
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
                artifact_refs[key] = {
                    "blobPath": self._upload_file(local_path, blob_path)}

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
            "blobPath": self._upload_file(
                a1_status_path,
                f"{blob_layout['logs']}/a1_status.json",
            )
        }

        for marker_name in ("a1_complete.json", "a1_failed.json", "a1_stopped.json"):
            marker_path = local_doc_dir / marker_name
            if marker_path.exists():
                artifact_refs["a1Marker"] = {
                    "blobPath": self._upload_file(
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
                            "blobPath": self._upload_file(image_path, blob_path),
                        }
                    )
        if uploaded_images:
            artifact_refs["images"] = uploaded_images

        self._save_backend_state(
            job_id=job_id,
            state_blob_path=state_blob_path,
            state=state,
            artifact_refs=artifact_refs,
        )
        return artifact_refs

    def _persist_s1_outputs(
        self,
        *,
        job_id: str,
        state_blob_path: str,
        pipeline_shared_state_path: str,
        s1_result: Any,
    ) -> dict[str, Any]:
        state = self._load_backend_state(
            job_id=job_id, state_blob_path=state_blob_path)
        blob_layout = state["blobLayout"]
        artifact_refs = state.setdefault("artifactRefs", {})
        local_doc_dir = Path(pipeline_shared_state_path).parent

        report_path = Path(
            s1_result.report_path) if s1_result.report_path else local_doc_dir / "s1_validation.json"
        if report_path.exists():
            artifact_refs["s1Validation"] = {
                "blobPath": self._upload_file(
                    report_path,
                    f"{blob_layout['output']}/s1_validation.json",
                )
            }

        shared_state_path = Path(pipeline_shared_state_path)
        if shared_state_path.exists():
            artifact_refs["pipelineSharedState"] = {
                "blobPath": self._upload_file(
                    shared_state_path,
                    f"{blob_layout['state']}/pipeline_shared_state.json",
                )
            }

        self._save_backend_state(
            job_id=job_id,
            state_blob_path=state_blob_path,
            state=state,
            artifact_refs=artifact_refs,
        )
        return artifact_refs

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
        state = self._load_backend_state(
            job_id=job_id, state_blob_path=state_blob_path)
        blob_layout = state["blobLayout"]
        artifact_refs = state.setdefault("artifactRefs", {})

        kc_plan_path = Path(pipeline_shared_state_path).parent / "kc_plan.json"

        # Resolve which docx to upload: prefer the post-S2 rendered path, fall back to
        # whatever A2 wrote directly (render_docx=True path, legacy callers).
        docx_local = Path(final_docx_path) if final_docx_path else (
            Path(a2_result.study_guide_docx) if a2_result.study_guide_docx else None
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
                artifact_refs[key] = {
                    "blobPath": self._upload_file(local_path, blob_path)}
                logger.info("Uploaded artifact %s → %s", key, blob_path)
            else:
                logger.warning("Artifact %s not found at %s — skipping", key, local_path)

        self._save_backend_state(
            job_id=job_id,
            state_blob_path=state_blob_path,
            state=state,
            artifact_refs=artifact_refs,
        )
        return artifact_refs

    @staticmethod
    def _llm_outline_from_to_data(to_data: dict[str, Any]) -> dict[str, Any]:
        """Convert the frontend toData shape back to llm_to_outline format.

        The frontend TO panel stores a cleaned/simplified structure:
          sections[].subtopics is a list of strings
        The pipeline expects llm_to_outline_classification.sections[].subtopics
        as a list of dicts with at least a "title" key.  We reconstruct that here.
        """
        sections = []
        for s in to_data.get("sections") or []:
            raw_subtopics = s.get("subtopics") or []
            sections.append({
                "title": s.get("title") or "",
                "word_count": s.get("word_count"),
                "minutes": s.get("duration_minutes"),
                "credit_hours": s.get("credit_hours"),
                "content": s.get("content_summary") or "",
                "interactive_elements": s.get("interactive_elements") or [],
                "subtopics": [
                    {"title": t} if isinstance(t, str) else t
                    for t in raw_subtopics
                ],
            })
        return {
            "course_title": to_data.get("course_name") or to_data.get("course_title") or "",
            "description": to_data.get("description") or "",
            "learning_objectives": to_data.get("learning_objectives") or [],
            "totals": {
                "word_count": to_data.get("total_word_count"),
                "minutes": to_data.get("total_minutes"),
                "credit_hours": to_data.get("total_credit_hours"),
            },
            "sections": sections,
            "_user_edited": True,
            "_reused_from_preview": True,
        }

    def _write_to_override_json(
        self,
        to_override: dict[str, Any],
        temp_dir: Path,
    ) -> Path:
        """Write the user-edited TO as a temp JSON file A0 can load directly.

        A0 detects a .json extension on to_outline_doc_path and loads it without
        making an LLM call, so the user's reviewed outline is used as-is and the
        TO extraction LLM call is skipped entirely.

        File format: { "llm_to_outline": <converted outline> }
        """
        to_json_path = temp_dir / "user_edited_to.json"
        payload = {"llm_to_outline": self._llm_outline_from_to_data(to_override)}
        to_json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("[pipeline_adapter] Wrote user-edited TO override → %s", to_json_path)
        return to_json_path

    def run_a0_a1(
        self,
        job_id: str,
        *,
        state_blob_path: str,
        prepared_inputs: dict[str, object] | None = None,
        s1_retry_feedback: dict[str, Any] | None = None,
        gate_attempt: int = 1,
    ) -> dict[str, Any]:
        inputs = prepared_inputs or self.prepare_inputs(
            job_id, state_blob_path=state_blob_path)
        temp_dir = Path(inputs["tempDir"])
        study_guide_path = Path(inputs["studyGuidePath"])
        raw_to_path = inputs.get("timedOutlinePath")
        timed_outline_path: Path | None = Path(raw_to_path) if raw_to_path else None

        # On the first gate cycle, if the user reviewed/edited the TO in the
        # three-panel step, write it as a local JSON file and pass it to A0.
        # A0 detects the .json extension → loads directly, skipping the LLM
        # TO extraction call.  Subsequent S1 retries (gate_attempt > 1) let A0
        # re-run fully so S1 feedback can be incorporated.
        effective_to_path: str | None = str(timed_outline_path) if timed_outline_path else None
        if gate_attempt == 1:
            backend_state = self._load_backend_state(
                job_id=job_id, state_blob_path=state_blob_path)
            to_override = backend_state.get("toOverride")
            if to_override and isinstance(to_override, dict):
                override_json = self._write_to_override_json(to_override, temp_dir)
                effective_to_path = str(override_json)

        a0_result = A0RequestSynthesizer(
            docx_path=str(study_guide_path),
            to_outline_doc_path=effective_to_path,
            output_dir=str(temp_dir),
        ).run()

        a1_feedback = dict(s1_retry_feedback) if s1_retry_feedback else None
        if a1_feedback is not None:
            a1_feedback.setdefault("gateAttempt", gate_attempt)

        a1_result = a1_run(
            shared_state_path=a0_result.shared_state_path,
            docx_path=str(study_guide_path),
            feedback=a1_feedback,
        )

        artifact_refs = self._persist_a0_a1_outputs(
            job_id=job_id,
            state_blob_path=state_blob_path,
            a0_result=a0_result,
            a1_result=a1_result,
        )

        return {
            "tempDir": str(temp_dir),
            "studyGuidePath": str(study_guide_path),
            "timedOutlinePath": str(timed_outline_path) if timed_outline_path else None,
            "a0": a0_result,
            "a1": a1_result,
            "a0SharedStatePath": a0_result.shared_state_path,
            "artifactRefs": artifact_refs,
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

    def _persist_s2_outputs(
        self,
        *,
        job_id: str,
        state_blob_path: str,
        pipeline_shared_state_path: str,
        s2_result: Any,
    ) -> dict[str, Any]:
        state = self._load_backend_state(
            job_id=job_id, state_blob_path=state_blob_path)
        blob_layout = state["blobLayout"]
        artifact_refs = state.setdefault("artifactRefs", {})

        report_path = (
            Path(s2_result.report_path)
            if s2_result.report_path
            else Path(pipeline_shared_state_path).parent / "s2_validation.json"
        )
        if report_path.exists():
            artifact_refs["s2Validation"] = {
                "blobPath": self._upload_file(
                    report_path,
                    f"{blob_layout['output']}/s2_validation.json",
                )
            }

        shared_state_path = Path(pipeline_shared_state_path)
        if shared_state_path.exists():
            artifact_refs["pipelineSharedState"] = {
                "blobPath": self._upload_file(
                    shared_state_path,
                    f"{blob_layout['state']}/pipeline_shared_state.json",
                )
            }

        self._save_backend_state(
            job_id=job_id,
            state_blob_path=state_blob_path,
            state=state,
            artifact_refs=artifact_refs,
        )
        return artifact_refs

    @staticmethod
    def _format_s2_feedback(report: Any) -> str:
        lines: list[str] = []
        if report.blockers:
            lines.append("Blockers (must fix):")
            for issue in report.issues:
                if issue.severity == "blocker":
                    lines.append(
                        f"  - [{issue.field}] {issue.message} (rule: {issue.rule_source})"
                    )
        if report.criticals:
            lines.append("Critical issues (must address):")
            for issue in report.issues:
                if issue.severity == "critical":
                    lines.append(
                        f"  - [{issue.field}] {issue.message} (rule: {issue.rule_source})"
                    )
        if report.warnings:
            lines.append("Warnings (please address):")
            for issue in report.issues:
                if issue.severity == "warning":
                    lines.append(
                        f"  - [{issue.field}] {issue.message} (rule: {issue.rule_source})"
                    )
        return "\n".join(lines)

    def run_a2(
        self,
        job_id: str,
        *,
        state_blob_path: str,
        pipeline_shared_state_path: str,
        study_guide_path: str,
        course_difficulty: str = "intermediate",
    ) -> dict[str, Any]:
        # ── Section Mapper ────────────────────────────────────────────────
        section_map_result = section_mapper_run(
            shared_state_path=pipeline_shared_state_path)
        enriched_sections_path = (
            Path(pipeline_shared_state_path).parent / "enriched_sections.json"
        )

        # ── KC Planner ────────────────────────────────────────────────────
        kc_result = kc_planner_run(shared_state_path=pipeline_shared_state_path)
        logger.info(
            "KC Planner: scenario=%s kc_count=%s",
            kc_result.get("scenario"),
            kc_result.get("kc_count"),
        )

        # ── A2 ↔ S2 loop (up to MAX_A2_S2_CYCLES) ────────────────────────
        a2_result: Any = None
        s2_result: Any = None
        a2_feedback: str | None = None

        for cycle in range(1, MAX_A2_S2_CYCLES + 1):
            logger.info("A2/S2 cycle %s/%s", cycle, MAX_A2_S2_CYCLES)

            a2_result = A2ContentGenerator(
                shared_state_path=pipeline_shared_state_path,
                docx_path=study_guide_path,
                render_docx=False,
                course_difficulty=course_difficulty,
                feedback=a2_feedback,
            ).run()
            logger.info(
                "A2 status=%s generated=%s skipped=%s failed=%s words=%s",
                a2_result.status,
                a2_result.stats.generated,
                a2_result.stats.skipped,
                a2_result.stats.failed,
                a2_result.stats.total_words,
            )

            s2_result = S2Validator(pipeline_shared_state_path).run()
            logger.info(
                "S2 status=%s blockers=%s warnings=%s",
                s2_result.status,
                s2_result.blockers,
                s2_result.warnings,
            )

            if s2_result.status not in ("blocked", "blocker"):
                logger.info("S2 passed — content cleared for DOCX rendering.")
                break

            if cycle < MAX_A2_S2_CYCLES:
                logger.warning(
                    "S2 blocked (cycle %s/%s) — regenerating A2 with feedback.",
                    cycle,
                    MAX_A2_S2_CYCLES,
                )
                a2_feedback = self._format_s2_feedback(s2_result)

        s2_hard_blocked = s2_result and s2_result.status in ("blocked", "blocker")

        # ── Render study_guide.docx (only when S2 passes) ─────────────────
        final_docx_path: str | None = None
        if not s2_hard_blocked:
            final_docx_path = render_study_guide_from_state(
                shared_state_path=pipeline_shared_state_path)
            logger.info("Study guide rendered -> %s", final_docx_path)
        else:
            logger.error(
                "S2 still blocked after %s cycle(s) — study_guide.docx NOT built.",
                MAX_A2_S2_CYCLES,
            )

        # ── Persist artifacts ─────────────────────────────────────────────
        artifact_refs = self._persist_a2_outputs(
            job_id=job_id,
            state_blob_path=state_blob_path,
            pipeline_shared_state_path=pipeline_shared_state_path,
            enriched_sections_path=enriched_sections_path,
            a2_result=a2_result,
            final_docx_path=final_docx_path,
        )
        if s2_result:
            self._persist_s2_outputs(
                job_id=job_id,
                state_blob_path=state_blob_path,
                pipeline_shared_state_path=pipeline_shared_state_path,
                s2_result=s2_result,
            )

        return {
            "sectionMap": section_map_result,
            "a2": a2_result,
            "s2": s2_result,
            "s2_hard_blocked": s2_hard_blocked,
            "artifactRefs": artifact_refs,
        }

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
        first_issue = s1_result.issues[0].message if getattr(
            s1_result, "issues", None) else "S1 validation blocked A2."
        payload = {
            "code": "S1_VALIDATION_BLOCKED",
            "message": first_issue,
            "stage": "S1",
            "retryable": False,
            "validationStatus": s1_result.status.value,
        }
        return json.dumps(payload)
