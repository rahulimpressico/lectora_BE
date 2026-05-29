"""Queue-driven orchestration for worker-side job handling."""
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from azure.servicebus import AutoLockRenewer, ServiceBusClient

from lectora_backend.config import settings
from lectora_backend.core.job_logger import JobLogger
from lectora_backend.core.pipeline_adapter import PipelineAdapter
from lectora_backend.core.state_manager import StateManager
from lectora_backend.dependencies import SessionLocal
from lectora_backend.models.job_enums import (
    JobStatus,
    PipelineStep,
    StageStatus,
    ValidationOutcome,
)
from lectora_backend.repositories.job_repository import JobRepository
from lectora_backend.models.constants import MAX_S1_GATE_CYCLES


logger = logging.getLogger(__name__)
MAX_MESSAGE_DELIVERIES = 3
MAX_LOCK_RENEWAL_DURATION_SECONDS = 30 * 60


class JobNotFoundError(Exception):
    """Raised when a queued message references a job that no longer exists."""


class Orchestrator:
    def __init__(self) -> None:
        self._client = ServiceBusClient.from_connection_string(
            conn_str=settings.service_bus_connection_string
        )
        self._queue_name = settings.queue_name
        self._lock_renewer = AutoLockRenewer(
            max_lock_renewal_duration=MAX_LOCK_RENEWAL_DURATION_SECONDS
        )
        self._state_manager = StateManager()
        self._pipeline_adapter = PipelineAdapter()

    def _start_stage(
        self,
        repository: JobRepository,
        job_id: str,
        state: dict,
        state_blob_path: str,
        stage_id: PipelineStep,
        started_at: datetime,
    ) -> None:
        repository.update_stage_status(
            job_id=job_id,
            stage_id=stage_id,
            status=StageStatus.PROCESSING,
            started_at=started_at,
        )

        state["run"]["updatedAt"] = started_at.isoformat()
        state["stageExecutionState"][stage_id.value] = {
            "status": StageStatus.PROCESSING.value,
            "startedAt": started_at.isoformat(),
            "completedAt": None,
        }
        self._state_manager.save(job_id, state, blob_path=state_blob_path)

    def _complete_stage(
        self,
        repository: JobRepository,
        job_id: str,
        state: dict,
        state_blob_path: str,
        stage_id: PipelineStep,
        started_at: datetime,
        completed_at: datetime,
        validation_outcome: ValidationOutcome | None = None,
    ) -> None:
        repository.update_stage_status(
            job_id=job_id,
            stage_id=stage_id,
            status=StageStatus.COMPLETED,
            completed_at=completed_at,
            validation_outcome=validation_outcome,
            error_detail=None,
        )

        state["run"]["updatedAt"] = completed_at.isoformat()
        state["stageExecutionState"][stage_id.value] = {
            "status": StageStatus.COMPLETED.value,
            "startedAt": started_at.isoformat(),
            "completedAt": completed_at.isoformat(),
        }
        if validation_outcome is not None:
            state["stageExecutionState"][stage_id.value]["outcome"] = (
                validation_outcome.value
            )
        self._state_manager.save(job_id, state, blob_path=state_blob_path)

    def _fail_stage(
        self,
        repository: JobRepository,
        job_id: str,
        state: dict,
        state_blob_path: str,
        stage_id: PipelineStep,
        started_at: datetime,
        completed_at: datetime,
        *,
        validation_outcome: ValidationOutcome | None = None,
        error_detail: str | None = None,
    ) -> None:
        repository.update_stage_status(
            job_id=job_id,
            stage_id=stage_id,
            status=StageStatus.FAILED,
            completed_at=completed_at,
            validation_outcome=validation_outcome,
            error_detail=error_detail,
        )

        state["run"]["updatedAt"] = completed_at.isoformat()
        state["stageExecutionState"][stage_id.value] = {
            "status": StageStatus.FAILED.value,
            "startedAt": started_at.isoformat(),
            "completedAt": completed_at.isoformat(),
        }
        if validation_outcome is not None:
            state["stageExecutionState"][stage_id.value]["outcome"] = (
                validation_outcome.value
            )
        if error_detail is not None:
            state["stageExecutionState"][stage_id.value]["errorDetail"] = error_detail
        self._state_manager.save(job_id, state, blob_path=state_blob_path)

    async def listen(self) -> None:
        logger.info("Orchestrator listening on queue %s", self._queue_name)

        with self._client:
            receiver = self._client.get_queue_receiver(
                queue_name=self._queue_name,
                max_wait_time=5,
            )
            with receiver:
                with self._lock_renewer:
                    while True:
                        logger.info("Polling queue %s for messages", self._queue_name)
                        messages = receiver.receive_messages(
                            max_message_count=1,
                            max_wait_time=5,
                        )

                        if not messages:
                            logger.info(
                                "No messages received from queue %s", self._queue_name
                            )
                            continue

                        logger.info(
                            "Received %s message(s) from queue %s",
                            len(messages),
                            self._queue_name,
                        )
                        for message in messages:
                            self._lock_renewer.register(
                                receiver,
                                message,
                                max_lock_renewal_duration=MAX_LOCK_RENEWAL_DURATION_SECONDS,
                            )

                            try:
                                raw_msg = str(message)
                                payload = json.loads(raw_msg)
                                job_id = payload["jobId"]
                                logger.info("Processing job %s from queue", job_id)
                                await self.run_job(job_id, payload)
                            except (json.JSONDecodeError, KeyError) as exc:
                                logger.exception(
                                    "Dead-lettering malformed message (raw=%r): %s",
                                    str(message)[:500],
                                    exc,
                                )
                                receiver.dead_letter_message(
                                    message,
                                    reason="MalformedMessage",
                                    error_description=str(exc),
                                )
                            except JobNotFoundError as exc:
                                logger.warning(
                                    "Dead-lettering orphan message: %s", exc
                                )
                                receiver.dead_letter_message(
                                    message,
                                    reason="JobNotFound",
                                    error_description=str(exc),
                                )
                            except Exception as exc:
                                delivery_count = getattr(
                                    message, "delivery_count", 1
                                )
                                logger.exception(
                                    "Worker failed for message on attempt %s: %s",
                                    delivery_count,
                                    exc,
                                )
                                if delivery_count >= MAX_MESSAGE_DELIVERIES:
                                    receiver.dead_letter_message(
                                        message,
                                        reason="ProcessingFailed",
                                        error_description=str(exc),
                                    )
                                else:
                                    receiver.abandon_message(message)
                            else:
                                receiver.complete_message(message)

    async def run_job(self, job_id: str, payload: dict) -> None:
        session = SessionLocal()
        prepared_inputs: dict[str, object] | None = None
        try:
            repository = JobRepository(session)
            job_log = JobLogger(job_id, session)

            job = repository.get_job(job_id)
            if job is None:
                raise JobNotFoundError(
                    f"Job {job_id} not found; dropping orphan message."
                )
            repository.update_job_status(job_id, JobStatus.PROCESSING)
            state_blob_path = job.shared_state_blob_path

            job_log.info("Pipeline started — preparing document inputs")

            state = self._state_manager.load(job_id, blob_path=state_blob_path)
            prepared_inputs = self._pipeline_adapter.prepare_inputs(
                job_id,
                state_blob_path=state_blob_path,
            )

            job_log.info("Inputs ready — beginning A0 → A1 → S1 gate cycles")

            # ── A0 → A1 → S1 gate ───────────────────────────────────────────
            # S1 validates combined A0 + A1 outputs.
            # If S1 blocks, re-run A0+A1 with prior S1 feedback — up to MAX_S1_GATE_CYCLES.
            a0_ctx: dict[str, Any] | None = None
            a1_ctx: dict[str, Any] | None = None
            s1_result: dict[str, Any] | None = None
            prior_s1_report: Any | None = None

            for gate_cycle in range(1, MAX_S1_GATE_CYCLES + 1):
                logger.info(
                    "Job %s: gate cycle %s/%s (A0 → A1 → S1)",
                    job_id,
                    gate_cycle,
                    MAX_S1_GATE_CYCLES,
                )
                job_log.info(
                    f"Gate cycle {gate_cycle}/{MAX_S1_GATE_CYCLES}: running document analysis → outline interpretation → content validation",
                    "A1",
                )

                state = self._state_manager.load(job_id, blob_path=state_blob_path)

                s1_retry_bundle = (
                    self._pipeline_adapter.build_s1_retry_feedback(prior_s1_report)
                    if prior_s1_report is not None
                    else None
                )

                # ── A0 ──────────────────────────────────────────────────────
                a0_started_at = datetime.now(timezone.utc)
                self._start_stage(
                    repository=repository,
                    job_id=job_id,
                    state=state,
                    state_blob_path=state_blob_path,
                    stage_id=PipelineStep.A0,
                    started_at=a0_started_at,
                )
                job_log.info(
                    "Document analysis started — extracting metadata, images, and rule family",
                    "A0",
                )

                a0_ctx = self._pipeline_adapter.run_a0(
                    job_id,
                    state_blob_path=state_blob_path,
                    prepared_inputs=prepared_inputs,
                    gate_attempt=gate_cycle,
                )
                state = self._state_manager.load(job_id, blob_path=state_blob_path)

                a0_completed_at = datetime.now(timezone.utc)
                self._complete_stage(
                    repository=repository,
                    job_id=job_id,
                    state=state,
                    state_blob_path=state_blob_path,
                    stage_id=PipelineStep.A0,
                    started_at=a0_started_at,
                    completed_at=a0_completed_at,
                )
                job_log.success("Document analysis complete", "A0")

                # ── A1 ──────────────────────────────────────────────────────
                a1_started_at = datetime.now(timezone.utc)
                self._start_stage(
                    repository=repository,
                    job_id=job_id,
                    state=state,
                    state_blob_path=state_blob_path,
                    stage_id=PipelineStep.A1,
                    started_at=a1_started_at,
                )
                job_log.info(
                    "Outline interpretation started — building enriched course spec", "A1"
                )

                a1_ctx = self._pipeline_adapter.run_a1(
                    job_id,
                    state_blob_path=state_blob_path,
                    a0_result=a0_ctx["a0"],
                    study_guide_path=a0_ctx["studyGuidePath"],
                    s1_retry_feedback=s1_retry_bundle,
                    gate_attempt=gate_cycle,
                )
                state = self._state_manager.load(job_id, blob_path=state_blob_path)

                a1_completed_at = datetime.now(timezone.utc)
                self._complete_stage(
                    repository=repository,
                    job_id=job_id,
                    state=state,
                    state_blob_path=state_blob_path,
                    stage_id=PipelineStep.A1,
                    started_at=a1_started_at,
                    completed_at=a1_completed_at,
                )
                job_log.success("Outline interpretation complete — course spec built", "A1")

                # ── S1 ──────────────────────────────────────────────────────
                state = self._state_manager.load(job_id, blob_path=state_blob_path)

                # Always update S1 started_at so retries show the most recent
                # attempt time rather than the first cycle's timestamp.
                s1_started_at = datetime.now(timezone.utc)
                self._start_stage(
                    repository=repository,
                    job_id=job_id,
                    state=state,
                    state_blob_path=state_blob_path,
                    stage_id=PipelineStep.S1,
                    started_at=s1_started_at,
                )
                job_log.info("Content validation gate started", "S1")

                s1_result = self._pipeline_adapter.run_s1(
                    job_id,
                    state_blob_path=state_blob_path,
                    pipeline_shared_state_path=a0_ctx["a0SharedStatePath"],
                )
                state = self._state_manager.load(job_id, blob_path=state_blob_path)
                state["stageExecutionState"].setdefault(PipelineStep.S1.value, {})[
                    "gateCycle"
                ] = gate_cycle
                self._state_manager.save(job_id, state, blob_path=state_blob_path)

                s1_status = s1_result["s1"].status

                if not self._pipeline_adapter.s1_status_blocks_pipeline(s1_status):
                    break  # S1 passed — exit gate loop

                # ── S1 blocked: surface blocker details + schedule retry ──
                s1_issues = getattr(s1_result["s1"], "issues", []) or []
                blocker_issues = [
                    i
                    for i in s1_issues
                    if getattr(i, "severity", "") in ("blocker", "critical")
                ]
                blocker_details = [
                    {
                        "severity": getattr(i, "severity", "blocker"),
                        "field": getattr(i, "field", None),
                        "message": getattr(i, "message", str(i)),
                    }
                    for i in blocker_issues
                ]
                blocker_summary = (
                    "; ".join(d["message"] for d in blocker_details[:3])
                    or "Validation failed"
                )

                job_log.warn(
                    f"Content validation blocked (attempt {gate_cycle}/{MAX_S1_GATE_CYCLES}): {blocker_summary}",
                    "S1",
                )

                repository.update_stage_status(
                    job_id=job_id,
                    stage_id=PipelineStep.S1,
                    status=StageStatus.PROCESSING,
                    error_detail=json.dumps({
                        "code": "S1_RETRYING",
                        "message": f"Validation blocked — attempt {gate_cycle}/{MAX_S1_GATE_CYCLES}",
                        "stage": "S1",
                        "retryable": gate_cycle < MAX_S1_GATE_CYCLES,
                        "gate_cycle": gate_cycle,
                        "blockers": blocker_details,
                    }),
                )

                prior_s1_report = s1_result["s1"]
                if gate_cycle < MAX_S1_GATE_CYCLES:
                    logger.warning(
                        "S1 blocked for %s on gate cycle %s/%s — re-running A0 and A1 with S1 feedback",
                        job_id,
                        gate_cycle,
                        MAX_S1_GATE_CYCLES,
                    )
                    job_log.info(
                        f"Retrying with validation feedback (attempt {gate_cycle + 1}/{MAX_S1_GATE_CYCLES})",
                        "S1",
                    )
            else:
                # All gate cycles exhausted — hard-fail the job.
                s1_completed_at = datetime.now(timezone.utc)
                s1_error_detail = self._pipeline_adapter.build_s1_error_detail(
                    s1_result["s1"]
                )
                self._fail_stage(
                    repository=repository,
                    job_id=job_id,
                    state=state,
                    state_blob_path=state_blob_path,
                    stage_id=PipelineStep.S1,
                    started_at=s1_started_at,
                    completed_at=s1_completed_at,
                    validation_outcome=ValidationOutcome.CRITICAL_FAIL,
                    error_detail=s1_error_detail,
                )
                repository.update_job_status(job_id, JobStatus.FAILED)
                job_log.error(
                    f"Content validation failed after {MAX_S1_GATE_CYCLES} attempts — pipeline stopped",
                    "S1",
                )
                return

            # S1 passed
            s1_outcome = self._pipeline_adapter.build_s1_outcome(s1_result["s1"])
            s1_completed_at = datetime.now(timezone.utc)
            self._complete_stage(
                repository=repository,
                job_id=job_id,
                state=state,
                state_blob_path=state_blob_path,
                stage_id=PipelineStep.S1,
                started_at=s1_started_at,
                completed_at=s1_completed_at,
                validation_outcome=s1_outcome,
            )
            outcome_label = (
                "with warnings" if s1_outcome == ValidationOutcome.WARNING else "clean"
            )
            job_log.success(f"Content validation passed ({outcome_label})", "S1")

            # ── A2 (Section Mapper → KC Planner → content generation → S2 gate) ──
            state = self._state_manager.load(job_id, blob_path=state_blob_path)
            a2_started_at = datetime.now(timezone.utc)
            self._start_stage(
                repository=repository,
                job_id=job_id,
                state=state,
                state_blob_path=state_blob_path,
                stage_id=PipelineStep.A2,
                started_at=a2_started_at,
            )
            job_log.info("Section mapping + KC planning started", "A2")
            job_log.info(
                "Content generation started — writing course sections with AI", "A2"
            )

            a2_result = self._pipeline_adapter.run_a2(
                job_id,
                state_blob_path=state_blob_path,
                pipeline_shared_state_path=a0_ctx["a0SharedStatePath"],
                study_guide_path=a0_ctx["studyGuidePath"],
            )
            state = self._state_manager.load(job_id, blob_path=state_blob_path)
            a2_completed_at = datetime.now(timezone.utc)

            if a2_result.get("s2_hard_blocked"):
                s2_error = json.dumps({
                    "code": "S2_VALIDATION_BLOCKED",
                    "message": "Quality assurance gate blocked generation after max retries.",
                    "stage": "S2",
                    "retryable": False,
                })
                self._fail_stage(
                    repository=repository,
                    job_id=job_id,
                    state=state,
                    state_blob_path=state_blob_path,
                    stage_id=PipelineStep.A2,
                    started_at=a2_started_at,
                    completed_at=a2_completed_at,
                    validation_outcome=ValidationOutcome.CRITICAL_FAIL,
                    error_detail=s2_error,
                )
                repository.update_stage_status(
                    job_id=job_id,
                    stage_id=PipelineStep.S2,
                    status=StageStatus.FAILED,
                    completed_at=a2_completed_at,
                    validation_outcome=ValidationOutcome.CRITICAL_FAIL,
                    error_detail=s2_error,
                )
                repository.update_job_status(job_id, JobStatus.FAILED)
                job_log.error(
                    "Quality assurance gate blocked generation after max retries — pipeline stopped",
                    "S2",
                )
                logger.error(
                    "Job %s failed: S2 hard-blocked after max retries.", job_id
                )
                return

            self._complete_stage(
                repository=repository,
                job_id=job_id,
                state=state,
                state_blob_path=state_blob_path,
                stage_id=PipelineStep.A2,
                started_at=a2_started_at,
                completed_at=a2_completed_at,
                validation_outcome=ValidationOutcome.PASS,
            )
            job_log.success("Content generation complete — all sections written", "A2")

            repository.update_stage_status(
                job_id=job_id,
                stage_id=PipelineStep.S2,
                status=StageStatus.COMPLETED,
                completed_at=a2_completed_at,
                validation_outcome=ValidationOutcome.PASS,
            )
            job_log.success("Quality assurance gate passed — study guide rendered", "S2")

            repository.update_job_status(job_id, JobStatus.COMPLETED)
            job_log.success("Course generation complete — pipeline finished successfully")

        finally:
            if prepared_inputs is not None:
                shutil.rmtree(Path(prepared_inputs["tempDir"]), ignore_errors=True)
            session.close()
