"""Pydantic schemas for the generate-TO endpoint."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class SourceAnalysis(BaseModel):
    """Per-document source analysis result — used in both the analyze-source response
    and as input to generate-to so the TO/LO generation is source-aware."""
    source_name: str = Field(alias="sourceName", description="Original file name.")
    source_role: str = Field(
        alias="sourceRole",
        description="Categorical role: 'primary_source', 'supporting_source', or 'reference_only'.",
    )
    importance: str = Field(
        default="medium",
        description="Inferred coverage weight: 'high', 'medium', or 'low'.",
    )
    extract_hint: str = Field(
        default="",
        alias="extractHint",
        description="User guidance on what to get from this source.",
    )
    main_topics: list[str] = Field(
        default_factory=list,
        alias="mainTopics",
        description="Key topics extracted from the document TOC.",
    )
    recommended_course_use: str = Field(
        default="",
        alias="recommendedCourseUse",
        description="LLM recommendation on how to use this source in the course.",
    )
    recommended_depth: str = Field(
        default="",
        alias="recommendedDepth",
        description="Recommended coverage depth: 'light', 'moderate', or 'comprehensive'.",
    )
    supports_learning_objectives: list[str] = Field(
        default_factory=list,
        alias="supportsLearningObjectives",
        description="Suggested learning objectives this source can support.",
    )
    ignore_or_reduce: list[str] = Field(
        default_factory=list,
        alias="ignoreOrReduce",
        description="Topics or sections that should be deprioritised or omitted.",
    )

    model_config = {"populate_by_name": True}


class SourceAnalysisRequest(BaseModel):
    """Body for POST /documents/analyze-source."""
    blob_path: str = Field(alias="blobPath", description="Uploaded-documents blob path for the source file.")
    source_role: str = Field(
        default="primary_source",
        alias="sourceRole",
        description="Categorical role: 'primary_source', 'supporting_source', or 'reference_only'.",
    )
    extract_hint: str | None = Field(
        default=None,
        alias="extractHint",
        description="What should we get from this source? User-provided extraction focus.",
    )
    importance: str | None = Field(
        default=None,
        description="Deprecated — inferred from source_role when omitted.",
    )

    model_config = {"populate_by_name": True}


class SourceAnalysisResponse(SourceAnalysis):
    """Response from POST /documents/analyze-source (extends SourceAnalysis with no extra fields)."""


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
    duration_hours: float | None = Field(
        default=None,
        alias="durationHours",
        description=(
            "Course duration selected by the user (e.g. 1.5, 3, 4.5 hours). "
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

    # ── Source-analysis results (pre-computed by the frontend) ────────────────
    source_analyses: list[SourceAnalysis] = Field(
        default_factory=list,
        alias="sourceAnalyses",
        description=(
            "Per-document source analysis results computed by POST /documents/analyze-source "
            "before generate-to is called. When present, A0 uses extract hints and source roles "
            "to weight TO/LO generation; ignore_or_reduce topics are deprioritised."
        ),
    )

    # ── Required topics (mandatory content areas from the wizard) ─────────────
    required_topics: list[str] = Field(
        default_factory=list,
        alias="requiredTopics",
        description=(
            "Mandatory course topics specified by the user in the wizard. "
            "Every topic in this list MUST appear in the generated TO. "
            "Treated as highest-priority content — overrides deprioritisation signals."
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
    s1_validation: dict[str, Any] | None = Field(default=None, alias="s1Validation")

    model_config = {"populate_by_name": True}


class GenerateTOJobAccepted(BaseModel):
    """Returned immediately by async POST /documents/generate-to (HTTP 202)."""
    job_id: str = Field(alias="jobId")
    status: str = "processing"
    message: str = "A0 started — pipeline runs A0, then S1, then A1"
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
    s1_validation: dict[str, Any] | None = Field(default=None, alias="s1Validation")
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
    source_analyses: list[SourceAnalysis] = Field(
        default_factory=list,
        alias="sourceAnalyses",
        description="Pre-computed source analysis results from POST /documents/analyze-source.",
    )
    required_topics: list[str] = Field(
        default_factory=list,
        alias="requiredTopics",
        description=(
            "Mandatory course topics from the wizard. "
            "Used to guide LO generation toward these required areas."
        ),
    )
    regeneration_prompt: str = Field(
        default="",
        alias="regenerationPrompt",
        description=(
            "Optional user instruction to guide LO regeneration "
            "(e.g., 'make it more advanced')."
        ),
    )
    current_objectives: list[str] = Field(
        default_factory=list,
        alias="currentObjectives",
        description=(
            "The existing learning objectives shown to the user before regeneration. "
            "When provided alongside regeneration_prompt, the LLM modifies these "
            "rather than generating from scratch."
        ),
    )
    model_config = {"populate_by_name": True}


class GenerateLearningObjectivesResponse(BaseModel):
    """Response from POST /documents/generate-learning-objectives."""
    learning_objectives: list[str] = Field(alias="learningObjectives")
    model_config = {"populate_by_name": True}


class SuggestRequiredTopicsRequest(BaseModel):
    """Body for POST /documents/suggest-required-topics."""
    course_title: str = Field(default="", alias="courseTitle")
    course_description: str = Field(default="", alias="courseDescription")
    course_type: str = Field(default="", alias="courseType")
    course_duration: str = Field(default="", alias="courseDuration")
    target_audience: str = Field(default="", alias="targetAudience")
    skill_level: str = Field(default="", alias="skillLevel")
    learner_outcomes: str = Field(default="", alias="learnerOutcomes")
    model_config = {"populate_by_name": True}


class SuggestRequiredTopicsResponse(BaseModel):
    """Response from POST /documents/suggest-required-topics."""
    required_topics: list[str] = Field(alias="requiredTopics")
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


class SuggestCourseTypeRequest(BaseModel):
    """Body for POST /documents/suggest-course-type."""
    course_title: str = Field(default="", alias="courseTitle")
    course_description: str = Field(default="", alias="courseDescription")
    target_audience: str = Field(default="", alias="targetAudience")
    learning_objectives: list[str] = Field(default_factory=list, alias="learningObjectives")
    model_config = {"populate_by_name": True}


class SuggestCourseTypeResponse(BaseModel):
    """Response from POST /documents/suggest-course-type."""
    rule_family: str = Field(alias="ruleFamily", description="Rule family key, e.g. 'insurance_ce'.")
    rule_family_label: str = Field(alias="ruleFamilyLabel", description="Display name, e.g. 'Insurance CE'.")
    confidence: float = Field(default=0.0, description="LLM confidence score (0–1).")
    reasoning: str = Field(default="", description="One-sentence explanation from the LLM.")
    model_config = {"populate_by_name": True}


class SaveTORequest(BaseModel):
    """Body for POST /documents/save-to — persist user-edited TO to blob storage."""
    blob_path: str = Field(
        alias="blobPath",
        description="The blob path originally returned by POST /documents/generate-to.",
    )
    to: dict[str, Any] = Field(description="Current FE-format Training Outline JSON.")
    rules: dict[str, Any] | None = Field(
        default=None,
        description="Current FE-format rules JSON (optional; pass through from store).",
    )
    model_config = {"populate_by_name": True}


class SaveTOResponse(BaseModel):
    """Response from POST /documents/save-to."""
    blob_path: str = Field(alias="blobPath", description="Confirmed path where the TO was saved.")
    model_config = {"populate_by_name": True}


class ReviseTORequest(BaseModel):
    """Body for POST /documents/revise-to."""
    current_to: dict[str, Any] = Field(
        alias="currentTo",
        description="The complete Training Outline JSON currently shown in the editor.",
    )
    revision_prompt: str = Field(
        alias="revisionPrompt",
        description="User's natural-language instruction describing the desired changes.",
    )
    model_config = {"populate_by_name": True}


class ReviseTOResponse(BaseModel):
    """Response from POST /documents/revise-to — contains only the revised TO."""
    to: dict[str, Any] = Field(description="The revised Training Outline JSON.")
    model_config = {"populate_by_name": True}
