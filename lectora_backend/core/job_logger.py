"""Per-job structured logging to the database.

Writes log entries synchronously to the job_logs table so they are
immediately visible to the SSE streaming endpoint.  Uses the same
SQLAlchemy session as the orchestrator so every write is inside the
caller's transaction context — each _write() issues its own commit
so clients can read the entries without waiting for a larger transaction.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from lectora_backend.models.db_models import JobLog

_py_logger = logging.getLogger(__name__)


class JobLogger:
    """Writes structured log entries for a specific job.

    Args:
        job_id: The job this logger is bound to.
        session: An open SQLAlchemy session (typically the orchestrator's session).
    """

    def __init__(self, job_id: str, session: Session) -> None:
        self._job_id = job_id
        self._session = session

    # ── Public API ─────────────────────────────────────────────────────────────

    def info(self, message: str, stage_id: str | None = None) -> None:
        self._write("info", message, stage_id)

    def success(self, message: str, stage_id: str | None = None) -> None:
        self._write("success", message, stage_id)

    def warn(self, message: str, stage_id: str | None = None) -> None:
        self._write("warn", message, stage_id)

    def error(self, message: str, stage_id: str | None = None) -> None:
        self._write("error", message, stage_id)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _write(self, level: str, message: str, stage_id: str | None) -> None:
        try:
            entry = JobLog(
                job_id=self._job_id,
                stage_id=stage_id,
                level=level,
                message=message,
            )
            self._session.add(entry)
            self._session.commit()
            _py_logger.debug(
                "[job=%s stage=%s] %s: %s", self._job_id, stage_id, level, message
            )
        except Exception as exc:
            _py_logger.warning(
                "Failed to write job log for job=%s: %s", self._job_id, exc
            )
            try:
                self._session.rollback()
            except Exception:
                pass
