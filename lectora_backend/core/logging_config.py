"""Central logging configuration for API + worker + pipeline agents.

Goal:
- consistent formatting (optionally JSON-ish via key=value fields)
- inject run_id/doc_name when available (from pipeline tracer context)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional


class _RunContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # These imports are intentionally inside the filter to avoid creating a
        # hard dependency from core -> pipeline at import time.
        run_id = ""
        doc_name = ""
        try:
            from lectora_backend.pipeline.shared_llm_config import tracer

            run_id = tracer.get_run_id()
            doc_name = tracer.get_doc_name()
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "RunContextFilter: could not load tracer context: %s", exc
            )

        if not hasattr(record, "run_id"):
            record.run_id = run_id
        if not hasattr(record, "doc_name"):
            record.doc_name = doc_name
        return True


@dataclass(frozen=True)
class LoggingOptions:
    level: str = "INFO"
    azure_sdk_level: str = "WARNING"
    fmt: str = (
        "%(asctime)s %(levelname)s %(name)s "
        "run_id=%(run_id)s doc_name=%(doc_name)s %(message)s"
    )


def configure_logging(options: Optional[LoggingOptions] = None) -> None:
    """Idempotent-ish logging setup (safe to call multiple times)."""
    opts = options or LoggingOptions(level=os.getenv("LOG_LEVEL", "INFO"))

    root = logging.getLogger()
    root.setLevel(getattr(logging, opts.level.upper(), logging.INFO))

    # Avoid duplicate handlers when called repeatedly (uvicorn reload, tests, etc.)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(opts.fmt))
        handler.addFilter(_RunContextFilter())
        root.addHandler(handler)

    # Ensure all application loggers under lectora_backend emit at the configured level
    app_level = getattr(logging, opts.level.upper(), logging.INFO)
    logging.getLogger("lectora_backend").setLevel(app_level)

    # Reduce Azure SDK noise (still overrideable by env if needed)
    logging.getLogger("azure").setLevel(
        getattr(logging, opts.azure_sdk_level.upper(), logging.WARNING)
    )
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(
        getattr(logging, opts.azure_sdk_level.upper(), logging.WARNING)
    )
    logging.getLogger("azure.servicebus").setLevel(
        getattr(logging, opts.azure_sdk_level.upper(), logging.WARNING)
    )

