"""Artifact-related response schemas."""
from datetime import datetime

from lectora_backend.api.schemas.base import CamelModel


class ArtifactSummary(CamelModel):
    type: str
    blob_path: str
    stage: str
    is_latest: bool
    created_at: datetime


class ArtifactListResponse(CamelModel):
    job_id: str
    artifacts: list[ArtifactSummary]
