"""Queue-driven orchestration runtime implementation."""

from datetime import datetime

from azure.servicebus import AutoLockRenewer, ServiceBusClient

from lectora_backend.config import settings
from lectora_backend.core.orchestration.errors import JobNotFoundError
from lectora_backend.core.orchestration.worker_loop import listen_for_messages, run_pipeline_job
from lectora_backend.core.pipeline.pipeline_adapter_runtime import PipelineAdapter
from lectora_backend.core.state_manager import StateManager
from lectora_backend.models.job_enums import (
    PipelineStep,
    StageStatus,
    ValidationOutcome,
)
from lectora_backend.repositories.job_repository import JobRepository

MAX_MESSAGE_DELIVERIES = 3
MAX_LOCK_RENEWAL_DURATION_SECONDS = (
    120 * 60
)  # 2 hours - covers worst-case 3xS1 + 3xS2 cycles


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
        await listen_for_messages(
            self,
            max_lock_renewal_duration_seconds=MAX_LOCK_RENEWAL_DURATION_SECONDS,
            max_message_deliveries=MAX_MESSAGE_DELIVERIES,
        )

    async def run_job(self, job_id: str, payload: dict) -> None:
        await run_pipeline_job(self, job_id, payload)

