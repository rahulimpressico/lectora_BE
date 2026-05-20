"""Append-only structured audit log entries."""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("audit")


class AuditLogger:
    def log(self, job_id: str, event: str, details: dict | None = None) -> None:
        logger.info(
            "AUDIT",
            extra={
                "job_id": job_id,
                "event": event,
                "ts": datetime.now(timezone.utc).isoformat(),
                "details": details or {},
            },
        )
