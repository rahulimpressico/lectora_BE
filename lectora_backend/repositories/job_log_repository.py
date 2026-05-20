"""Read-side repository for job_logs — used by the SSE endpoint."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectora_backend.models.db_models import JobLog

_MAX_LOGS_PER_POLL = 200


class JobLogRepository:
    """Fetches log entries for SSE streaming."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_logs_since(
        self,
        job_id: str,
        after_id: int = 0,
        limit: int = _MAX_LOGS_PER_POLL,
    ) -> list[JobLog]:
        """Return log entries for *job_id* whose id is strictly greater than *after_id*.

        Results are ordered by id ascending so clients receive them in emission order.
        """
        stmt = (
            select(JobLog)
            .where(JobLog.job_id == job_id, JobLog.id > after_id)
            .order_by(JobLog.id)
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())
