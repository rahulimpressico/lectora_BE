"""Pydantic model for the in-flight shared_state document."""
from pydantic import BaseModel, Field


class SharedState(BaseModel):
    job_id: str = ""
    payload: dict = Field(default_factory=dict)
    course_spec: dict = Field(default_factory=dict)
    outline: dict = Field(default_factory=dict)
    generated_content: dict = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
