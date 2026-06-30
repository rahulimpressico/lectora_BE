from __future__ import annotations

from .base import *

class InputDoc(BaseModel):
    blob_path: str = Field(alias="blobPath")

    model_config = {"populate_by_name": True}

class JobInputs(BaseModel):
    study_guide: InputDoc = Field(alias="studyGuide")
    timed_outline: InputDoc | None = Field(default=None, alias="timedOutline")

    model_config = {"populate_by_name": True}

class SourceFileSpecPayload(BaseModel):
    blob_path: str = Field(alias="blobPath")
    extract_hint: str | None = Field(default=None, alias="extractHint")
    importance: str | None = Field(
        default=None,
        alias="importance",
        description="Deprecated — inferred from source role when omitted.",
    )

    model_config = {"populate_by_name": True}

class CourseConfigPayload(BaseModel):
    """Onboarding wizard fields forwarded to A2 for dynamic prompt construction."""
    # User-provided title and description — always the single source of truth.
    course_title: str | None = Field(default=None, alias="courseTitle")
    course_description: str | None = Field(default=None, alias="courseDescription")
    experience_level: str | None = Field(default=None, alias="experienceLevel")
    learner_outcomes: str | None = Field(default=None, alias="learnerOutcomes")
    audience_notes: str | None = Field(default=None, alias="audienceNotes")
    learning_objectives: list[str] = Field(default_factory=list, alias="learningObjectives")
    tone: str | None = Field(default=None)
    depth: str | None = Field(default=None)
    emphasis: str | None = Field(default=None)
    avoid: str | None = Field(default=None)
    include_scenarios: bool | None = Field(default=None, alias="includeScenarios")
    include_knowledge_checks: bool | None = Field(default=None, alias="includeKnowledgeChecks")

    model_config = {"populate_by_name": True}

class CreateJobPayload(BaseModel):
    course_title: str = Field(alias="courseTitle")
    course_type: str = Field(alias="courseType")
    difficulty: str = Field(default="intermediate")
    inputs: JobInputs
    to_override: dict[str, Any] | None = Field(default=None, alias="toOverride")
    # Per-file source specs (blob path + optional extraction focus).
    source_file_specs: list[SourceFileSpecPayload] | None = Field(default=None, alias="sourceFileSpecs")
    # Target audience — drives prompt calibration in A2 content generation.
    audience: str = Field(default="", alias="audience")
    # Optional special instructions provided by the user before course generation.
    # Injected into A2 prompts to influence tone, depth, and emphasis.
    special_instructions: str | None = Field(default=None, alias="specialInstructions")
    # All wizard onboarding fields for dynamic A2 prompt construction.
    course_config: CourseConfigPayload | None = Field(default=None, alias="courseConfig")

    model_config = {"populate_by_name": True}

class AIOperationPayload(BaseModel):
    operation: str
    section_id: str = Field(alias="sectionId")
    content: str | None = None
    context: dict[str, Any] | None = None
    user_prompt: str | None = Field(None, alias="userPrompt")

    model_config = {"populate_by_name": True}

class ReorderSectionsPayload(BaseModel):
    section_order: list[str] = Field(alias="sectionOrder")

    model_config = {"populate_by_name": True}

class SaveSectionPayload(BaseModel):
    content: str
    section_type: str | None = Field(None, alias="sectionType")
    title: str | None = None

    model_config = {"populate_by_name": True}

class UpdateCourseTitlePayload(BaseModel):
    course_title: str = Field(alias="courseTitle")

    model_config = {"populate_by_name": True}

class CourseSectionInput(BaseModel):
    id: str
    title: str
    level: int
    section_type: str = Field(alias="sectionType", default="content")
    content: str = ""
    paragraphs: list[dict[str, Any]] = Field(default_factory=list)
    learning_objectives: list[str] = Field(default_factory=list, alias="learningObjectives")
    word_count: int = Field(default=0, alias="wordCount")
    has_knowledge_check: bool = Field(default=False, alias="hasKnowledgeCheck")
    order: int = 0
    children: list["CourseSectionInput"] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

class SyncCoursePayload(BaseModel):
    course_title: str = Field(alias="courseTitle")
    sections: list[CourseSectionInput]
    meta: dict = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

class SaveToAzurePayload(BaseModel):
    course_title: str | None = Field(None, alias="courseTitle")
    course_slug: str | None = Field(None, alias="courseSlug")
    section_order: list[str] | None = Field(None, alias="sectionOrder")

    model_config = {"populate_by_name": True}


CourseSectionInput.model_rebuild()
