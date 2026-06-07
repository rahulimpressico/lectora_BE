"""Enumerations shared across the codebase."""
import enum


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StageStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PipelineStep(str, enum.Enum):
    A0 = "A0"
    A1 = "A1"
    S1 = "S1"
    SECTION_MAPPER = "SECTION_MAPPER"
    KC_PLANNER = "KC_PLANNER"
    A2 = "A2"
    S2 = "S2"


class ValidationOutcome(str, enum.Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    RECOVERABLE_FAIL = "RECOVERABLE_FAIL"
    CRITICAL_FAIL = "CRITICAL_FAIL"
