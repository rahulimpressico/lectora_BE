"""Artifact-related response schemas."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


def to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class ArtifactSummary(CamelModel):
    type: str
    blob_path: str
    stage: str
    is_latest: bool
    created_at: datetime


class ArtifactListResponse(CamelModel):
    job_id: str
    artifacts: list[ArtifactSummary]
