from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from lectora_backend.api.schemas.costing_schemas import (
    CostingSummaryResponse,
    CostingTrendPointResponse,
    DocumentCostResponse,
    ModelUsageResponse,
)
from lectora_backend.core.costing_service import CostingService


router = APIRouter()


@router.get("/summary", response_model=CostingSummaryResponse)
async def get_costing_summary() -> CostingSummaryResponse:
    return CostingService().get_summary()


@router.get("/models", response_model=list[ModelUsageResponse])
async def get_costing_models() -> list[ModelUsageResponse]:
    return CostingService().get_model_summary()


@router.get("/trends", response_model=list[CostingTrendPointResponse])
async def get_costing_trends() -> list[CostingTrendPointResponse]:
    return CostingService().get_cost_trends()


@router.get("/documents", response_model=list[DocumentCostResponse])
async def get_costing_documents() -> list[DocumentCostResponse]:
    return CostingService().list_documents()


@router.get("/documents/{document_id}", response_model=DocumentCostResponse)
async def get_costing_document(document_id: str) -> DocumentCostResponse:
    document = CostingService().get_document(document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown costing document: {document_id}",
        )
    return document
