import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lectora_backend.core.job_logger import JobLogger
from lectora_backend.dependencies import SessionLocal
from lectora_backend.models.constants import MAX_S1_GATE_CYCLES
from lectora_backend.models.job_enums import (
    JobStatus,
    PipelineStep,
    StageStatus,
    ValidationOutcome,
)
from lectora_backend.repositories.job_repository import JobRepository

from .errors import JobNotFoundError

logger = logging.getLogger(__name__)


async def listen_for_messages(
    orchestrator: Any,
    *,
    max_lock_renewal_duration_seconds: int,
    max_message_deliveries: int,
) -> None:
    logger.info("Orchestrator listening on queue %s", orchestrator._queue_name)

    with orchestrator._client:
        receiver = orchestrator._client.get_queue_receiver(
            queue_name=orchestrator._queue_name,
            max_wait_time=5,
        )
        with receiver:
            with orchestrator._lock_renewer:
                while True:
                    logger.info("Polling queue %s for messages", orchestrator._queue_name)
                    messages = await asyncio.to_thread(
                        receiver.receive_messages,
                        max_message_count=1,
                        max_wait_time=5,
                    )

                    if not messages:
                        logger.info(
                            "No messages received from queue %s", orchestrator._queue_name
                        )
                        continue

                    logger.info(
                        "Received %s message(s) from queue %s",
                        len(messages),
                        orchestrator._queue_name,
                    )
                    for message in messages:
                        orchestrator._lock_renewer.register(
                            receiver,
                            message,
                            max_lock_renewal_duration=max_lock_renewal_duration_seconds,
                        )

                        try:
                            raw_msg = str(message)
                            payload = json.loads(raw_msg)
                            job_id = payload["jobId"]
                            logger.info("Processing job %s from queue", job_id)
                            await orchestrator.run_job(job_id, payload)
                        except (json.JSONDecodeError, KeyError) as exc:
                            logger.exception(
                                "Dead-lettering malformed message (raw=%r): %s",
                                str(message)[:500],
                                exc,
                            )
                            await asyncio.to_thread(
                                receiver.dead_letter_message,
                                message,
                                reason="MalformedMessage",
                                error_description=str(exc),
                            )
                        except JobNotFoundError as exc:
                            logger.warning("Dead-lettering orphan message: %s", exc)
                            await asyncio.to_thread(
                                receiver.dead_letter_message,
                                message,
                                reason="JobNotFound",
                                error_description=str(exc),
                            )
                        except Exception as exc:
                            delivery_count = getattr(message, "delivery_count", 1)
                            logger.exception(
                                "Worker failed for message on attempt %s: %s",
                                delivery_count,
                                exc,
                            )
                            if delivery_count >= max_message_deliveries:
                                await asyncio.to_thread(
                                    receiver.dead_letter_message,
                                    message,
                                    reason="ProcessingFailed",
                                    error_description=str(exc),
                                )
                            else:
                                await asyncio.to_thread(receiver.abandon_message, message)
                        else:
                            await asyncio.to_thread(receiver.complete_message, message)


async def run_pipeline_job(orchestrator: Any, job_id: str, payload: dict) -> None:
    session = SessionLocal()
    prepared_inputs: dict[str, object] | None = None
    try:
        repository = JobRepository(session)
        job_log = JobLogger(job_id, session)

        job = repository.get_job(job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found; dropping orphan message.")
        # Idempotency guard — skip redelivered messages for already-terminal jobs
        if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED):
            logger.info(
                "[orchestrator] Job %s already %s — skipping redelivered message",
                job_id,
                job.status.value,
            )
            return
        if job.status == JobStatus.PROCESSING:
            logger.warning(
                "[orchestrator] Job %s already PROCESSING — possible duplicate delivery, skipping",
                job_id,
            )
            return
        repository.update_job_status(job_id, JobStatus.PROCESSING)
        # Re-check for cancellation that arrived between the status guard and PROCESSING write
        session.commit()
        job = repository.get_job(job_id)
        if job and job.status == JobStatus.CANCELLED:
            logger.info("[orchestrator] Job %s was cancelled — aborting pipeline start", job_id)
            return
        state_blob_path = job.shared_state_blob_path

        job_log.info("Pipeline started — preparing document inputs")

        state = orchestrator._state_manager.load(job_id, blob_path=state_blob_path)
        prepared_inputs = orchestrator._pipeline_adapter.prepare_inputs(
            job_id,
            state_blob_path=state_blob_path,
        )

        from lectora_backend.pipeline.shared_llm_config.tracer import set_run_context

        state = prepared_inputs["state"]
        course_title = (state.get("request") or {}).get("courseTitle") or ""
        study_guide_path = str(prepared_inputs["studyGuidePath"])
        doc_name = Path(study_guide_path).stem or course_title.replace(" ", "_")
        source_refs = list(state.get("source_file_paths") or [])
        if study_guide_path and study_guide_path not in source_refs:
            source_refs.insert(0, study_guide_path)
        set_run_context(job_id, doc_name, source_refs=source_refs)

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

            state = orchestrator._state_manager.load(job_id, blob_path=state_blob_path)

            s1_retry_bundle = (
                orchestrator._pipeline_adapter.build_s1_retry_feedback(prior_s1_report)
                if prior_s1_report is not None
                else None
            )

            # ── A0 ──────────────────────────────────────────────────────
            a0_started_at = datetime.now(timezone.utc)
            orchestrator._start_stage(
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

            a0_ctx = orchestrator._pipeline_adapter.run_a0(
                job_id,
                state_blob_path=state_blob_path,
                prepared_inputs=prepared_inputs,
                gate_attempt=gate_cycle,
            )
            state = orchestrator._state_manager.load(job_id, blob_path=state_blob_path)

            a0_completed_at = datetime.now(timezone.utc)
            orchestrator._complete_stage(
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
            orchestrator._start_stage(
                repository=repository,
                job_id=job_id,
                state=state,
                state_blob_path=state_blob_path,
                stage_id=PipelineStep.A1,
                started_at=a1_started_at,
            )
            job_log.info("Outline interpretation started — building enriched course spec", "A1")

            a1_ctx = orchestrator._pipeline_adapter.run_a1(
                job_id,
                state_blob_path=state_blob_path,
                a0_result=a0_ctx["a0"],
                study_guide_path=a0_ctx["studyGuidePath"],
                s1_retry_feedback=s1_retry_bundle,
                gate_attempt=gate_cycle,
            )
            state = orchestrator._state_manager.load(job_id, blob_path=state_blob_path)

            a1_completed_at = datetime.now(timezone.utc)
            orchestrator._complete_stage(
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
            state = orchestrator._state_manager.load(job_id, blob_path=state_blob_path)

            # Always update S1 started_at so retries show the most recent
            # attempt time rather than the first cycle's timestamp.
            s1_started_at = datetime.now(timezone.utc)
            orchestrator._start_stage(
                repository=repository,
                job_id=job_id,
                state=state,
                state_blob_path=state_blob_path,
                stage_id=PipelineStep.S1,
                started_at=s1_started_at,
            )
            job_log.info("Content validation gate started", "S1")

            s1_result = orchestrator._pipeline_adapter.run_s1(
                job_id,
                state_blob_path=state_blob_path,
                pipeline_shared_state_path=a0_ctx["a0SharedStatePath"],
            )
            state = orchestrator._state_manager.load(job_id, blob_path=state_blob_path)
            state["stageExecutionState"].setdefault(PipelineStep.S1.value, {})[
                "gateCycle"
            ] = gate_cycle
            orchestrator._state_manager.save(job_id, state, blob_path=state_blob_path)

            s1_status = s1_result["s1"].status

            if not orchestrator._pipeline_adapter.s1_status_blocks_pipeline(s1_status):
                break  # S1 passed — exit gate loop

            # ── S1 blocked: surface blocker details + schedule retry ──
            s1_issues = getattr(s1_result["s1"], "issues", []) or []
            blocker_issues = [
                i for i in s1_issues if getattr(i, "severity", "") in ("blocker", "critical")
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
                "; ".join(d["message"] for d in blocker_details[:3]) or "Validation failed"
            )

            job_log.warn(
                f"Content validation blocked (attempt {gate_cycle}/{MAX_S1_GATE_CYCLES}): {blocker_summary}",
                "S1",
            )

            repository.update_stage_status(
                job_id=job_id,
                stage_id=PipelineStep.S1,
                status=StageStatus.PROCESSING,
                error_detail=json.dumps(
                    {
                        "code": "S1_RETRYING",
                        "message": f"Validation blocked — attempt {gate_cycle}/{MAX_S1_GATE_CYCLES}",
                        "stage": "S1",
                        "retryable": gate_cycle < MAX_S1_GATE_CYCLES,
                        "gate_cycle": gate_cycle,
                        "blockers": blocker_details,
                    }
                ),
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
            s1_error_detail = orchestrator._pipeline_adapter.build_s1_error_detail(s1_result["s1"])
            orchestrator._fail_stage(
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
        s1_outcome = orchestrator._pipeline_adapter.build_s1_outcome(s1_result["s1"])
        s1_completed_at = datetime.now(timezone.utc)
        orchestrator._complete_stage(
            repository=repository,
            job_id=job_id,
            state=state,
            state_blob_path=state_blob_path,
            stage_id=PipelineStep.S1,
            started_at=s1_started_at,
            completed_at=s1_completed_at,
            validation_outcome=s1_outcome,
        )
        outcome_label = "with warnings" if s1_outcome == ValidationOutcome.WARNING else "clean"
        job_log.success(f"Content validation passed ({outcome_label})", "S1")

        # ── A2 (Section Mapper → KC Planner → content generation → S2 gate) ──
        state = orchestrator._state_manager.load(job_id, blob_path=state_blob_path)
        a2_started_at = datetime.now(timezone.utc)
        orchestrator._start_stage(
            repository=repository,
            job_id=job_id,
            state=state,
            state_blob_path=state_blob_path,
            stage_id=PipelineStep.A2,
            started_at=a2_started_at,
        )
        job_log.info("Section mapping + KC planning started", "A2")
        job_log.info("Content generation started — writing course sections with AI", "A2")

        a2_result = orchestrator._pipeline_adapter.run_a2(
            job_id,
            state_blob_path=state_blob_path,
            pipeline_shared_state_path=a0_ctx["a0SharedStatePath"],
            study_guide_path=a0_ctx["studyGuidePath"],
            course_difficulty=a0_ctx.get("courseDifficulty", "intermediate"),
        )
        state = orchestrator._state_manager.load(job_id, blob_path=state_blob_path)
        a2_completed_at = datetime.now(timezone.utc)

        if a2_result.get("s2_hard_blocked"):
            s2_error = json.dumps(
                {
                    "code": "S2_VALIDATION_BLOCKED",
                    "message": "Quality assurance gate blocked generation after max retries.",
                    "stage": "S2",
                    "retryable": False,
                }
            )
            orchestrator._fail_stage(
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
            logger.error("Job %s failed: S2 hard-blocked after max retries.", job_id)
            return

        orchestrator._complete_stage(
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

    except JobNotFoundError:
        raise  # Let listen() handle dead-lettering; do not mark as FAILED
    except Exception as exc:
        logger.exception("[orchestrator] run_job %s unhandled error: %s", job_id, exc)
        try:
            _repo = JobRepository(session)
            _repo.update_job_status(job_id, JobStatus.FAILED)
            session.commit()
        except Exception:
            pass
        raise
    finally:
        if prepared_inputs is not None:
            shutil.rmtree(Path(prepared_inputs["tempDir"]), ignore_errors=True)
        try:
            session.rollback()
        except Exception:
            pass
        session.close()
