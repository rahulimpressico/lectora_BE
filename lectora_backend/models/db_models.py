"""SQLAlchemy ORM models for jobs and stages."""
from sqlalchemy import Column, String, ForeignKey, Integer, Text, DateTime, Enum as SAEnum, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from lectora_backend.models.job_enums import (
    JobStatus,
    PipelineStep,
    StageStatus,
    ValidationOutcome,
)
from datetime import datetime, timezone


class Base(DeclarativeBase):
    pass

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class Job(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[JobStatus] = mapped_column(SAEnum(JobStatus), nullable=False)
    course_title: Mapped[str] = mapped_column(String(255), nullable=False)
    course_type: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    shared_state_blob_path:Mapped["str"]= mapped_column(String(512), nullable=False)
    created_at : Mapped[datetime] = mapped_column (DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at : Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    stage_progress: Mapped[list["StageProgress"]] = relationship(
        "StageProgress",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="StageProgress.id",
    )
    retry_history: Mapped[list["RetryHistory"]] = relationship(
        "RetryHistory",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="RetryHistory.attempt",
    )
    logs: Mapped[list["JobLog"]] = relationship(
        "JobLog",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobLog.id",
    )


class StageProgress(Base):
    __tablename__ = "stage_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), nullable=False, index=True)
    stage_id: Mapped[PipelineStep] = mapped_column(SAEnum(PipelineStep), nullable=False)
    status: Mapped[StageStatus] = mapped_column(SAEnum(StageStatus), nullable=False)
    validation_outcome: Mapped[ValidationOutcome | None] = mapped_column(
        SAEnum(ValidationOutcome),
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped["Job"] = relationship("Job", back_populates="stage_progress")


class RetryHistory(Base):
    __tablename__ = "retry_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    from_stage: Mapped[PipelineStep] = mapped_column(SAEnum(PipelineStep), nullable=False)
    section_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(255), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )
    overrides: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[StageStatus] = mapped_column(SAEnum(StageStatus), nullable=False)

    job: Mapped["Job"] = relationship("Job", back_populates="retry_history")
    __table_args__ = (UniqueConstraint('job_id', 'attempt', name='uq_retry_job_attempt'),)


class JobLog(Base):
    """Structured per-job log stream, written by the orchestrator and streamed via SSE."""
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False, index=True)
    stage_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    job: Mapped["Job"] = relationship("Job", back_populates="logs")

