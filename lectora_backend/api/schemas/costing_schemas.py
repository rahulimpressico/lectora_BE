from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ModelUsageResponse(BaseModel):
    modelId: str
    modelName: str
    inputTokens: int
    outputTokens: int
    totalRequests: int
    cost: float


class StageBreakdownResponse(BaseModel):
    stageKey: Literal[
        "to_generation",
        "content_generation",
        "assessment_generation",
        "image_generation",
        "metadata_generation",
        "search_operations",
        "other",
    ]
    stageName: str
    inputTokens: int
    outputTokens: int
    cost: float
    requests: int


class DocumentCostResponse(BaseModel):
    documentId: str
    documentName: str
    documentType: str
    status: Literal["completed", "in-progress", "failed"]
    totalCost: float
    inputTokens: int
    outputTokens: int
    totalRequests: int
    modelsUsed: list[str]
    lastUpdated: str
    modelBreakdown: list[ModelUsageResponse]
    stageBreakdown: list[StageBreakdownResponse]


class CostingTrendPointResponse(BaseModel):
    date: str
    cost: float
    inputTokens: int
    outputTokens: int


class CostingSummaryResponse(BaseModel):
    totalCost: float
    totalInputTokens: int
    totalOutputTokens: int
    totalDocumentsProcessed: int
    averageCostPerDocument: float
    estimatedMonthlyCost: float
    costTrend: list[CostingTrendPointResponse]
    modelSummary: list[ModelUsageResponse]
    documents: list[DocumentCostResponse]
    costChangePercent: float
    documentsChangePercent: float
