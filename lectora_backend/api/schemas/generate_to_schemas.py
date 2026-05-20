"""Pydantic schemas for the generate-TO endpoint."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UploadDocumentResponse(BaseModel):
    """Returned by POST /documents/upload."""
    blob_path: str = Field(alias="blobPath")
    upload_folder: str = Field(
        alias="uploadFolder",
        description="Sanitized folder name under uploaded-documents/ (from course topic).",
    )

    model_config = {"populate_by_name": True}


class GenerateTORequest(BaseModel):
    """Body accepted by POST /documents/generate-to."""
    blob_path: str = Field(alias="blobPath")
    difficulty: str = "intermediate"

    model_config = {"populate_by_name": True}


class GenerateTOResponse(BaseModel):
    """
    Returned when A0 finishes (sync mode or poll when ``status`` is ``completed``).

    ``to``         — flat+nested dict the UI renders in the middle panel.
    ``rules``      — resolved rule pack dict the UI renders in the right panel.
    ``toBlobPath`` — local path to the saved generated-TO JSON file; pass this as
                     ``inputs.timedOutline.blobPath`` in POST /jobs so the main
                     pipeline can reuse the same TO instead of re-generating it.
    Both ``to`` and ``rules`` are schema-free; RecursiveJsonEditor renders them dynamically.
    """
    to: dict[str, Any]
    rules: dict[str, Any]
    to_blob_path: str | None = Field(default=None, alias="toBlobPath")


class GenerateTOJobAccepted(BaseModel):
    """Returned immediately by async POST /documents/generate-to (HTTP 202)."""
    job_id: str = Field(alias="jobId")
    status: str = "processing"
    message: str = "A0 started — poll until complete"
    poll_url: str = Field(alias="pollUrl")

    model_config = {"populate_by_name": True}


class GenerateTOJobPollResponse(BaseModel):
    """Returned by GET /documents/generate-to/jobs/{jobId}."""
    job_id: str = Field(alias="jobId")
    status: str  # processing | completed | failed
    message: str | None = None
    error: str | None = None
    to: dict[str, Any] | None = None
    rules: dict[str, Any] | None = None
    to_blob_path: str | None = Field(default=None, alias="toBlobPath")

    model_config = {"populate_by_name": True}
