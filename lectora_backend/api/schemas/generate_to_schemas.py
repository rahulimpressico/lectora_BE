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
    document_id: str = Field(
        alias="documentId",
        description="Unique ID for this uploaded document. Poll GET /documents/{documentId}/ingestion-status.",
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

    course_title: str | None = Field(
        default=None,
        alias="courseTitle",
        description=(
            "User-provided course title — single source of truth. "
            "When present, overrides any LLM-generated title in the TO response."
        ),
    )

    audience: str | None = Field(
        default=None,
        alias="audience",
        description=(
            "Target audience for the course (e.g. 'Trained Insurance Agents', "
            "'New Agents', 'Business Owners'). When provided, the TO generation "
            "and content writing are calibrated to this audience."
        ),
    )

    learning_objectives: list[str] = Field(
        default_factory=list,
        alias="learningObjectives",
        description="Explicit learning objectives from the wizard; merged into the A0 prompt context.",
    )
    preferred_chapters: int | None = Field(
        default=None,
        alias="preferredChapters",
        description="User-preferred number of course chapters/sections.",
    )
    lesson_style: str | None = Field(
        default=None,
        alias="lessonStyle",
        description="Lesson style preference: 'short' (compact) or 'detailed' (comprehensive).",
    )

    # ── Extended onboarding fields ────────────────────────────────────────────
    course_description: str | None = Field(
        default=None,
        alias="courseDescription",
        description="Short description of what this course covers, written by the user in the wizard.",
    )
    experience_level: str | None = Field(
        default=None,
        alias="experienceLevel",
        description="Learner experience level: 'new', 'some', or 'experienced'.",
    )
    learner_outcomes: str | None = Field(
        default=None,
        alias="learnerOutcomes",
        description="Free-text statement of what learners should be able to do after the course.",
    )
    audience_notes: str | None = Field(
        default=None,
        alias="audienceNotes",
        description="Additional learner context: industry background, prior knowledge, regulatory sensitivity.",
    )
    tone: str | None = Field(
        default=None,
        description="Desired writing tone (e.g. 'Professional', 'Conversational', 'Academic').",
    )
    depth: str | None = Field(
        default=None,
        description="Course depth preference: 'overview', 'balanced', or 'detailed'.",
    )
    emphasis: str | None = Field(
        default=None,
        description="Topics or concepts to emphasise throughout the course.",
    )
    avoid: str | None = Field(
        default=None,
        description="Topics, language, or approaches the course should explicitly avoid.",
    )
    include_scenarios: bool | None = Field(
        default=None,
        alias="includeScenarios",
        description="Whether to include real-world scenarios and examples.",
    )
    include_knowledge_checks: bool | None = Field(
        default=None,
        alias="includeKnowledgeChecks",
        description="Whether to include knowledge check questions.",
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


class GenerateLearningObjectivesRequest(BaseModel):
    """Body for POST /documents/generate-learning-objectives."""
    source_materials: list[str] = Field(default_factory=list, alias="sourceMaterials")
    course_title: str = Field(default="", alias="courseTitle")
    course_description: str = Field(default="", alias="courseDescription")
    course_type: str = Field(default="", alias="courseType")
    course_duration: str = Field(default="", alias="courseDuration")
    target_audience: str = Field(default="", alias="targetAudience")
    skill_level: str = Field(default="", alias="skillLevel")
    desired_outcomes: str = Field(default="", alias="desiredOutcomes")
    certification_focus: str = Field(default="", alias="certificationFocus")
    additional_instructions: str = Field(default="", alias="additionalInstructions")
    model_config = {"populate_by_name": True}


class GenerateLearningObjectivesResponse(BaseModel):
    """Response from POST /documents/generate-learning-objectives."""
    learning_objectives: list[str] = Field(alias="learningObjectives")
    model_config = {"populate_by_name": True}


class SuggestOutlineStructureRequest(BaseModel):
    """Body for POST /documents/suggest-outline-structure."""
    course_title: str = Field(default="", alias="courseTitle")
    course_description: str = Field(default="", alias="courseDescription")
    course_type: str = Field(default="", alias="courseType")
    target_audience: str = Field(default="", alias="targetAudience")
    skill_level: str = Field(default="", alias="skillLevel")
    learning_objectives: list[str] = Field(default_factory=list, alias="learningObjectives")
    model_config = {"populate_by_name": True}


class SuggestOutlineStructureResponse(BaseModel):
    """Response from POST /documents/suggest-outline-structure."""
    preferred_chapters: int = Field(alias="preferredChapters")
    lesson_style: str = Field(alias="lessonStyle")
    reasoning: str = Field(default="", alias="reasoning")
    model_config = {"populate_by_name": True}
