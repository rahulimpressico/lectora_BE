"""Pydantic schemas for the generate-TO endpoint."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class UploadDocumentResponse(BaseModel):
    """Returned by POST /documents/upload."""
    blob_path: str = Field(alias="blobPath")
    upload_folder: str = Field(
        alias="uploadFolder",
        description="Sanitized folder name under uploaded-documents/ (from course topic).",
    )

    model_config = {"populate_by_name": True}


class GenerateTORequest(BaseModel):
    """Body accepted by POST /documents/generate-to.

    Supports both the legacy single-file shape and the new multi-file shape:

    Legacy:   { "blobPath": "folder/file.docx", "difficulty": "intermediate" }
    Multi:    { "blobPaths": ["folder/a.docx", "folder/b.pdf"], "difficulty": "...",
                "customToPrompt": "..." }

    New dynamic flow (when user selects duration + difficulty in the UI):
      {
        "blobPaths": [...],
        "durationHours": 3,
        "difficultyLevel": "advanced",
        "calculatedWordCount": 18000
      }
    When ``durationHours`` and ``calculatedWordCount`` are provided, A0 skips
    TOC/heading extraction and sends file content directly to the LLM with a
    dynamic prompt built from the course configuration.

    At least one of ``blobPath`` or ``blobPaths`` must be provided.
    ``blobPath`` is kept for backward compatibility; ``blobPaths`` takes precedence.
    """
    blob_path: str | None = Field(default=None, alias="blobPath")
    blob_paths: list[str] = Field(default_factory=list, alias="blobPaths")
    difficulty: str = "intermediate"
    custom_to_prompt: str | None = Field(
        default=None,
        alias="customToPrompt",
        description=(
            "Optional custom system prompt for TO generation. "
            "When provided, replaces the default internal GENERATE_TO_PROMPT. "
            "The response JSON schema remains unchanged."
        ),
    )
    course_type_hint: str | None = Field(
        default=None,
        alias="courseTypeHint",
        description=(
            "Optional domain/course-type context (e.g. 'Washington LTC Compliance Course'). "
            "Used to prioritize relevant topics and filter unrelated content during TO generation."
        ),
    )
    to_doc_blob_path: str | None = Field(
        default=None,
        alias="toDocBlobPath",
        description="Optional user-uploaded TO document blob path (DOCX or PDF). When provided, A0 parses this as the Timed Outline instead of generating one from scratch.",
    )

    # ── New dynamic TO generation params ─────────────────────────────────────
    duration_hours: int | None = Field(
        default=None,
        alias="durationHours",
        description=(
            "Course duration selected by the user (1–5 hours). "
            "When provided together with calculatedWordCount, activates the new "
            "dynamic TO generation flow (raw file content → LLM with config prompt)."
        ),
    )
    difficulty_level: str | None = Field(
        default=None,
        alias="difficultyLevel",
        description=(
            "Difficulty level selected by the user: 'basic', 'intermediate', or 'advanced'. "
            "Used in the dynamic TO prompt for word count and credit hour calculations."
        ),
    )
    calculated_word_count: int | None = Field(
        default=None,
        alias="calculatedWordCount",
        description=(
            "Word count target calculated by the frontend: "
            "(duration_hours × 9000) / difficulty_multiplier. "
            "Embedded in the dynamic TO prompt so the LLM distributes words correctly."
        ),
    )

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _normalise_paths(self) -> "GenerateTORequest":
        # Merge legacy blobPath into blobPaths for uniform downstream handling.
        if self.blob_path and self.blob_path not in self.blob_paths:
            self.blob_paths = [self.blob_path] + list(self.blob_paths)
        if not self.blob_paths:
            raise ValueError("At least one of 'blobPath' or 'blobPaths' must be provided.")
        return self

    @property
    def effective_blob_paths(self) -> list[str]:
        """Normalised list of all blob paths (always non-empty after validation)."""
        return self.blob_paths


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
    status: str  # processing | completed | failed | cancelled
    message: str | None = None
    error: str | None = None
    to: dict[str, Any] | None = None
    rules: dict[str, Any] | None = None
    to_blob_path: str | None = Field(default=None, alias="toBlobPath")
    logs: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True}
