"""Schemas for the dashboard summary API."""
from pydantic import BaseModel, Field


class DashboardSummaryResponse(BaseModel):
    courses_generated: int = Field(..., alias="coursesGenerated", ge=0)
    in_progress: int = Field(..., alias="inProgress", ge=0)
    completed: int = Field(..., ge=0)

    model_config = {
        "populate_by_name": True,
    }
