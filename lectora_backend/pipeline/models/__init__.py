"""
Lectora — Pydantic model registry.

All production-ready models are re-exported here so any agent can do:

    from lectora_backend.pipeline.models import RequestSpec, ValidationIssue, KnowledgeCheckBlock

Import groups
─────────────
Rule Pack      : RulePack and all sub-models
Course (A0)    : RequestSpec, SharedState, A0Result, …
Outline (A1)   : SectionSpec, CourseSpec, A1Output, …
Validation (S1): ValidationIssue, S1ValidationReport, …
Validation (S2): S2ValidationReport, S2Status, …
Content (A2)   : BodyParagraph union + all block types, GeneratedSection, A2Output
TO Outline     : TOOutline, TOSection, TOTotals
Pipeline       : PipelineResult
"""

# ── Rule Pack ─────────────────────────────────────────────────────────────────
from .rule_pack import (
    AssessmentRules,
    ComplianceElements,
    ContentRules,
    DeduplicationRules,
    DisclosureHandling,
    ErrorTolerance,
    KcPlacementRules,
    LectoraConstraints,
    QuestionFormatDistribution,
    RulePack,
    StyleConstraints,
)

# ── Course / A0 ───────────────────────────────────────────────────────────────
from .course import (
    A0OutputFiles,
    A0Result,
    AgentOutputSlots,
    ContentGenerationBounds,
    CourseMetadata,
    ExtractedInputs,
    ImageRecord,
    LLMClassification,
    PipelineStatus,
    ProvenanceEntry,
    ProvenanceSource,
    RequestSpec,
    ResolvedAssessmentRules,
    RuleClassification,
    SharedState,
)

# ── Outline / A1 ──────────────────────────────────────────────────────────────
from .outline import (
    A1Output,
    A1Status,
    CourseSpec,
    ImagePlacement,
    Inconsistency,
    InconsistencySeverity,
    SectionSpec,
)

# ── Validation / S1 ───────────────────────────────────────────────────────────
from .validation import (
    IssueSeverity,
    S1Status,
    S1ValidationReport,
    S2Status,
    S2ValidationReport,
    ValidationIssue,
)

# ── Content / A2 ─────────────────────────────────────────────────────────────
from .content import (
    A2Output,
    A2Stats,
    BodyParagraph,
    BulletListBlock,
    GeneratedSection,
    GeneratedSectionStatus,
    Heading3Block,
    Heading4Block,
    ImportantCalloutBlock,
    KnowledgeCheckBlock,
    NumberedListBlock,
    SubBulletListBlock,
    TextBlock,
)

# ── TO Outline ────────────────────────────────────────────────────────────────
from .to_outline import (
    TOOutline,
    TOSection,
    TOTotals,
)

# ── Pipeline ──────────────────────────────────────────────────────────────────
from .pipeline import PipelineResult

__all__ = [
    # Rule Pack
    "AssessmentRules",
    "ComplianceElements",
    "ContentRules",
    "DeduplicationRules",
    "DisclosureHandling",
    "ErrorTolerance",
    "KcPlacementRules",
    "LectoraConstraints",
    "QuestionFormatDistribution",
    "RulePack",
    "StyleConstraints",
    # Course / A0
    "A0OutputFiles",
    "A0Result",
    "AgentOutputSlots",
    "ContentGenerationBounds",
    "CourseMetadata",
    "ExtractedInputs",
    "ImageRecord",
    "LLMClassification",
    "PipelineStatus",
    "ProvenanceEntry",
    "ProvenanceSource",
    "RequestSpec",
    "ResolvedAssessmentRules",
    "RuleClassification",
    "SharedState",
    # Outline / A1
    "A1Output",
    "A1Status",
    "CourseSpec",
    "ImagePlacement",
    "Inconsistency",
    "InconsistencySeverity",
    "SectionSpec",
    # Validation / S1 + S2
    "IssueSeverity",
    "S1Status",
    "S1ValidationReport",
    "S2Status",
    "S2ValidationReport",
    "ValidationIssue",
    # Content / A2
    "A2Output",
    "A2Stats",
    "BodyParagraph",
    "BulletListBlock",
    "GeneratedSection",
    "GeneratedSectionStatus",
    "Heading3Block",
    "Heading4Block",
    "ImportantCalloutBlock",
    "KnowledgeCheckBlock",
    "NumberedListBlock",
    "SubBulletListBlock",
    "TextBlock",
    # TO Outline
    "TOOutline",
    "TOSection",
    "TOTotals",
    # Pipeline
    "PipelineResult",
]
