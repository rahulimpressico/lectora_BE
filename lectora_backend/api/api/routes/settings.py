"""
Settings API — read and write pipeline configuration.

Endpoints
---------
GET  /settings          → current agent model configs + available models
PUT  /settings/models   → update deployment for one or more agents
POST /settings/models/reset → revert all agents to their default deployments
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from lectora_backend.pipeline.shared_llm_config.model_registry import (
    AGENT_META,
    AVAILABLE_MODELS,
    get_all_configs,
    reset_all_deployments,
    reset_deployment,
    set_deployment,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class ModelUpdate(BaseModel):
    agent_id: str
    deployment: str

    @field_validator("agent_id")
    @classmethod
    def agent_must_exist(cls, v: str) -> str:
        if v not in AGENT_META:
            raise ValueError(f"Unknown agent_id '{v}'. Valid: {list(AGENT_META)}")
        return v

    @field_validator("deployment")
    @classmethod
    def deployment_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("deployment must not be empty")
        return v.strip()


class BulkModelUpdate(BaseModel):
    updates: list[ModelUpdate]


class ResetRequest(BaseModel):
    agent_id: str | None = None   # None → reset all


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
def get_settings() -> dict:
    """
    Return the full settings payload:
    - per-agent model configs (default + current + override flag)
    - list of available models for the dropdowns
    """
    return {
        "agents": get_all_configs(),
        "available_models": AVAILABLE_MODELS,
    }


@router.put("/models")
def update_models(payload: BulkModelUpdate) -> dict:
    """
    Persist deployment overrides for one or more agents.

    Changes take effect immediately — the next pipeline run picks them up
    without a server restart.
    """
    for update in payload.updates:
        set_deployment(update.agent_id, update.deployment)

    return {
        "status": "ok",
        "message": f"Updated {len(payload.updates)} agent(s).",
        "agents": get_all_configs(),
    }


@router.post("/models/reset")
def reset_models(body: ResetRequest | None = None) -> dict:
    """
    Revert deployment overrides to defaults.

    - Pass `{"agent_id": "A1"}` to reset a specific agent.
    - Omit body (or pass `{}`) to reset all agents.
    """
    if body and body.agent_id:
        if body.agent_id not in AGENT_META:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown agent_id '{body.agent_id}'. Valid: {list(AGENT_META)}",
            )
        reset_deployment(body.agent_id)
        message = f"Agent {body.agent_id} reset to default."
    else:
        reset_all_deployments()
        message = "All agents reset to defaults."

    return {
        "status": "ok",
        "message": message,
        "agents": get_all_configs(),
    }
