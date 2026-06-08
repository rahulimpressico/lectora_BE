"""In-memory store for local (dev) course generation pipeline jobs.

Mirrors the pattern established by generate_to_job_store.py — thread-safe,
TTL-based expiry, concurrency slot control.  Used exclusively by the
local_jobs router so the full pipeline can run without Azure infrastructure.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


_TTL_SECONDS = 7200  # 2 hours
_MAX_CONCURRENT_JOBS = 3


class LocalJobStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class LocalStageProgress:
    stage_id: str
    status: str = "PENDING"
    started_at: str | None = None
    completed_at: str | None = None
    outcome: str | None = None
    blockers: list[dict[str, Any]] = field(default_factory=list)
    retry_attempt: int = 0
    error_message: str | None = None  # persisted by fail_stage()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage_id,
            "status": self.status,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "outcome": self.outcome,
            "blockers": self.blockers,
            "retryAttempt": self.retry_attempt,
            "errorMessage": self.error_message,
        }


@dataclass
class LocalLogEntry:
    id: int
    level: str  # info | warn | error | success
    message: str
    stage_id: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "level": self.level,
            "message": self.message,
            "stageId": self.stage_id,
            "createdAt": self.created_at,
        }


@dataclass
class LocalCourseJob:
    job_id: str
    course_title: str
    course_type: str
    difficulty: str
    status: LocalJobStatus
    created_at: str
    updated_at: str
    stages: list[LocalStageProgress] = field(default_factory=list)
    logs: list[LocalLogEntry] = field(default_factory=list)
    shared_state_path: str | None = None
    study_guide_path: str | None = None
    temp_dir: str | None = None
    azure_blob_root: str | None = None
    artifact_dir: str | None = None
    input_docx_path: str | None = None  # path to original uploaded study guide (needed for regenerate)
    error: dict[str, Any] | None = None
    finished_at: float | None = None
    _log_counter: int = field(default=0, repr=False)

    def append_log(self, level: str, message: str, stage_id: str | None = None) -> None:
        from datetime import datetime, timezone
        self._log_counter += 1
        self.logs.append(LocalLogEntry(
            id=self._log_counter,
            level=level,
            message=message,
            stage_id=stage_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        ))

    def get_stage(self, stage_id: str) -> LocalStageProgress | None:
        return next((s for s in self.stages if s.stage_id == stage_id), None)


# Ordered list of pipeline stages used to initialise stage progress records.
PIPELINE_STAGES = ["A0", "A1", "S1", "SECTION_MAPPER", "KC_PLANNER", "A2", "S2"]


class LocalCourseJobStore:
    """Thread-safe in-memory store for local pipeline jobs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, LocalCourseJob] = {}
        self._active_slots = 0

    def acquire_slot(self) -> bool:
        with self._lock:
            if self._active_slots >= _MAX_CONCURRENT_JOBS:
                return False
            self._active_slots += 1
            return True

    def release_slot(self) -> None:
        with self._lock:
            self._active_slots = max(0, self._active_slots - 1)

    def active_count(self) -> int:
        with self._lock:
            return self._active_slots

    def create(
        self,
        *,
        course_title: str,
        course_type: str,
        difficulty: str,
    ) -> LocalCourseJob:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        job = LocalCourseJob(
            job_id=uuid.uuid4().hex,
            course_title=course_title,
            course_type=course_type,
            difficulty=difficulty,
            status=LocalJobStatus.PENDING,
            created_at=now,
            updated_at=now,
            stages=[
                LocalStageProgress(stage_id=s) for s in PIPELINE_STAGES
            ],
        )
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> LocalCourseJob | None:
        self._evict_expired()
        with self._lock:
            return self._jobs.get(job_id)

    def update_status(self, job_id: str, status: LocalJobStatus) -> None:
        from datetime import datetime, timezone
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = status
                job.updated_at = datetime.now(timezone.utc).isoformat()

    def start_stage(self, job_id: str, stage_id: str) -> None:
        from datetime import datetime, timezone
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            stage = job.get_stage(stage_id)
            if stage:
                stage.status = "PROCESSING"
                stage.started_at = datetime.now(timezone.utc).isoformat()
            job.updated_at = datetime.now(timezone.utc).isoformat()

    def complete_stage(
        self,
        job_id: str,
        stage_id: str,
        outcome: str,
        *,
        blockers: list[dict[str, Any]] | None = None,
        retry_attempt: int = 0,
    ) -> None:
        from datetime import datetime, timezone
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            stage = job.get_stage(stage_id)
            if stage:
                stage.status = "COMPLETED"
                stage.completed_at = datetime.now(timezone.utc).isoformat()
                stage.outcome = outcome
                stage.blockers = blockers or []
                stage.retry_attempt = retry_attempt
            job.updated_at = datetime.now(timezone.utc).isoformat()

    def fail_stage(
        self,
        job_id: str,
        stage_id: str,
        error_message: str,
    ) -> None:
        from datetime import datetime, timezone
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            stage = job.get_stage(stage_id)
            if stage:
                stage.status = "FAILED"
                stage.completed_at = datetime.now(timezone.utc).isoformat()
                stage.error_message = error_message  # surfaced in to_dict()
            job.updated_at = datetime.now(timezone.utc).isoformat()

    def append_log(
        self,
        job_id: str,
        level: str,
        message: str,
        stage_id: str | None = None,
    ) -> None:
        from lectora_backend.core.pipeline_run_log import flush_job_logs, sync_run_log_to_azure

        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.append_log(level, message, stage_id)
            if job.artifact_dir:
                flush_job_logs(job)
        # Azure log upload outside lock (network I/O)
        job_ref = self.get(job_id)
        if job_ref and job_ref.artifact_dir:
            sync_run_log_to_azure(job_ref)

    def complete_job(
        self,
        job_id: str,
        *,
        shared_state_path: str | None = None,
        study_guide_path: str | None = None,
    ) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = LocalJobStatus.COMPLETED
            job.updated_at = now
            job.shared_state_path = shared_state_path
            job.study_guide_path = study_guide_path
            job.finished_at = time.monotonic()

    def cancel_job(self, job_id: str, *, reason: str = "Cancelled") -> bool:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status in (
                LocalJobStatus.COMPLETED,
                LocalJobStatus.FAILED,
                LocalJobStatus.CANCELLED,
            ):
                return False
            job.status = LocalJobStatus.CANCELLED
            job.updated_at = now
            job.error = {"message": reason, "code": "CANCELLED"}
            job.finished_at = time.monotonic()
            if job.temp_dir:
                import shutil
                from pathlib import Path

                shutil.rmtree(Path(job.temp_dir), ignore_errors=True)
                job.temp_dir = None
            return True

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return job is not None and job.status == LocalJobStatus.CANCELLED

    def fail_job(
        self,
        job_id: str,
        *,
        error: dict[str, Any] | None = None,
        current_stage: str | None = None,
    ) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.status == LocalJobStatus.CANCELLED:
                return
            job.status = LocalJobStatus.FAILED
            job.updated_at = now
            job.error = error or {"message": "Pipeline failed"}
            job.finished_at = time.monotonic()
            if current_stage:
                stage = job.get_stage(current_stage)
                if stage and stage.status not in ("COMPLETED", "FAILED"):
                    stage.status = "FAILED"
                    stage.completed_at = now

    def set_temp_dir(self, job_id: str, temp_dir: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.temp_dir = temp_dir

    def set_input_docx(self, job_id: str, path: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.input_docx_path = path

    def update_study_guide_path(self, job_id: str, path: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.study_guide_path = path

    def set_azure_blob_root(self, job_id: str, root: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.azure_blob_root = root.rstrip("/") + "/"

    def set_artifact_dir(self, job_id: str, path: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.artifact_dir = path

    def register_from_filesystem(
        self,
        *,
        job_id: str,
        course_title: str,
        course_type: str,
        shared_state_path: str | None,
        study_guide_path: str | None = None,
        temp_dir: str | None = None,
        azure_blob_root: str | None = None,
    ) -> LocalCourseJob:
        """Register a COMPLETED job reconstructed from on-disk artifacts.

        Called after a server restart when the in-memory store is empty but
        pipeline output files still exist (e.g. shared_state.json on disk).
        Only inserts if the job_id is not already present.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        job = LocalCourseJob(
            job_id=job_id,
            course_title=course_title,
            course_type=course_type,
            difficulty="intermediate",
            status=LocalJobStatus.COMPLETED,
            created_at=now,
            updated_at=now,
            shared_state_path=shared_state_path,
            study_guide_path=study_guide_path,
            temp_dir=temp_dir,
            azure_blob_root=azure_blob_root,
            stages=[LocalStageProgress(stage_id=s, status="COMPLETED") for s in PIPELINE_STAGES],
        )
        with self._lock:
            if job_id not in self._jobs:
                self._jobs[job_id] = job
        return self._jobs[job_id]

    def list_all(self) -> list[LocalCourseJob]:
        """Return a snapshot of all non-expired jobs."""
        self._evict_expired()
        with self._lock:
            return list(self._jobs.values())

    def remove(self, job_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def _evict_expired(self) -> None:
        cutoff = time.monotonic() - _TTL_SECONDS
        with self._lock:
            expired = [
                jid for jid, j in self._jobs.items()
                if j.finished_at is not None and j.finished_at < cutoff
            ]
            for jid in expired:
                del self._jobs[jid]


_store_instance: LocalCourseJobStore | None = None
_store_lock = threading.Lock()


def get_local_course_job_store() -> LocalCourseJobStore:
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                _store_instance = LocalCourseJobStore()
    return _store_instance
