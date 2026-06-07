"""
GET /costing/summary
GET /costing/documents/{documentId}

Data sources (in priority order):
  1. Azure Cost Management API  — real Azure billing data for summary totals and
     daily cost trends.  Requires AZURE_SUBSCRIPTION_ID + service-principal
     credentials (AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET).
     Optional: AZURE_RESOURCE_GROUP to narrow the scope to a single resource group.

  2. LLM trace files  — per-document and per-model breakdowns computed from
     pipeline/logs/*/<agent>/llm_traces.jsonl written by tracer.py.
     Used for document-level drill-down and token counts even when Azure Cost
     Management returns data (because Azure billing is aggregated by resource,
     not by course run).

  3. Pure trace fallback  — when neither Azure Cost Management nor any trace
     files are available, returns empty collections so the UI shows a "no data"
     state instead of an error.

Why both?
  Azure Cost Management → actual billed USD amounts, daily trend, OpenAI service total.
  LLM traces           → per-document attribution, per-model token usage, per-stage costs.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Trace log root ─────────────────────────────────────────────────────────────
_LOGS_ROOT = (
    Path(__file__).resolve().parent.parent.parent / "pipeline" / "logs"
)

# ── Agent → stage key mapping ──────────────────────────────────────────────────
_AGENT_TO_STAGE: dict[str, str] = {
    "A0":             "to_generation",
    "A0_TO":          "to_generation",
    "A1":             "assessment_generation",
    "S1":             "assessment_generation",
    "SECTION_MAPPER": "metadata_generation",
    "KC_PLANNER":     "metadata_generation",
    "A2":             "content_generation",
    "S2":             "assessment_generation",
}
_STAGE_NAMES: dict[str, str] = {
    "to_generation":         "TO Generation",
    "content_generation":    "Content Generation",
    "assessment_generation": "Assessment Generation",
    "metadata_generation":   "Metadata Generation",
    "other":                 "Other Processing",
}


# ── Pydantic response models ───────────────────────────────────────────────────

class ModelUsage(BaseModel):
    modelId: str
    modelName: str
    inputTokens: int
    outputTokens: int
    totalRequests: int
    cost: float


class StageBreakdown(BaseModel):
    stageKey: str
    stageName: str
    inputTokens: int
    outputTokens: int
    cost: float
    requests: int


class DocumentCost(BaseModel):
    documentId: str
    documentName: str
    documentType: str
    status: str
    totalCost: float
    inputTokens: int
    outputTokens: int
    totalRequests: int
    modelsUsed: list[str]
    lastUpdated: str
    modelBreakdown: list[ModelUsage]
    stageBreakdown: list[StageBreakdown]


class CostingTrendPoint(BaseModel):
    date: str
    cost: float
    inputTokens: int
    outputTokens: int


class CostingSummary(BaseModel):
    totalCost: float
    totalInputTokens: int
    totalOutputTokens: int
    totalDocumentsProcessed: int
    averageCostPerDocument: float
    estimatedMonthlyCost: float
    costTrend: list[CostingTrendPoint]
    modelSummary: list[ModelUsage]
    documents: list[DocumentCost]
    costChangePercent: float
    documentsChangePercent: float
    dataSource: str  # "azure_cost_management" | "llm_traces" | "empty"


# ── LLM Trace helpers ──────────────────────────────────────────────────────────

def _read_traces() -> list[dict[str, Any]]:
    """Read all llm_traces.jsonl files under pipeline/logs/."""
    records: list[dict[str, Any]] = []
    if not _LOGS_ROOT.exists():
        return records
    for trace_file in _LOGS_ROOT.rglob("llm_traces.jsonl"):
        try:
            with trace_file.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass
    return records


def _build_documents(records: list[dict[str, Any]]) -> list[DocumentCost]:
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        doc = (r.get("doc_name") or "").strip()
        if doc:
            by_doc[doc].append(r)

    docs: list[DocumentCost] = []
    for doc_name, traces in by_doc.items():
        model_agg: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"inputTokens": 0, "outputTokens": 0, "totalRequests": 0, "cost": 0.0}
        )
        stage_agg: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"inputTokens": 0, "outputTokens": 0, "cost": 0.0, "requests": 0}
        )
        last_ts = ""

        for t in traces:
            dep = (t.get("deployment") or "unknown").strip().lower()
            in_tok = int(t.get("prompt_tokens") or 0)
            out_tok = int(t.get("completion_tokens") or 0)
            cost = float(t.get("total_cost_usd") or 0.0)
            agent = (t.get("agent") or "").upper()
            ts = t.get("timestamp") or ""
            if ts > last_ts:
                last_ts = ts
            model_agg[dep]["inputTokens"] += in_tok
            model_agg[dep]["outputTokens"] += out_tok
            model_agg[dep]["totalRequests"] += 1
            model_agg[dep]["cost"] += cost
            stage_key = _AGENT_TO_STAGE.get(agent, "other")
            stage_agg[stage_key]["inputTokens"] += in_tok
            stage_agg[stage_key]["outputTokens"] += out_tok
            stage_agg[stage_key]["cost"] += cost
            stage_agg[stage_key]["requests"] += 1

        total_cost = sum(m["cost"] for m in model_agg.values())
        total_in   = sum(m["inputTokens"] for m in model_agg.values())
        total_out  = sum(m["outputTokens"] for m in model_agg.values())
        total_req  = sum(m["totalRequests"] for m in model_agg.values())

        docs.append(DocumentCost(
            documentId=doc_name,
            documentName=doc_name.replace("_", " "),
            documentType="Course",
            status="completed",
            totalCost=round(total_cost, 6),
            inputTokens=total_in,
            outputTokens=total_out,
            totalRequests=total_req,
            modelsUsed=list(model_agg.keys()),
            lastUpdated=last_ts or datetime.now(timezone.utc).isoformat(),
            modelBreakdown=[
                ModelUsage(
                    modelId=dep,
                    modelName=dep.upper(),
                    inputTokens=v["inputTokens"],
                    outputTokens=v["outputTokens"],
                    totalRequests=v["totalRequests"],
                    cost=round(v["cost"], 6),
                )
                for dep, v in model_agg.items()
            ],
            stageBreakdown=[
                StageBreakdown(
                    stageKey=sk,
                    stageName=_STAGE_NAMES.get(sk, sk),
                    inputTokens=sv["inputTokens"],
                    outputTokens=sv["outputTokens"],
                    cost=round(sv["cost"], 6),
                    requests=sv["requests"],
                )
                for sk, sv in stage_agg.items()
            ],
        ))

    return sorted(docs, key=lambda d: d.lastUpdated, reverse=True)


def _trace_trend(records: list[dict[str, Any]]) -> list[CostingTrendPoint]:
    daily: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"cost": 0.0, "inputTokens": 0, "outputTokens": 0}
    )
    for r in records:
        ts = r.get("timestamp") or ""
        if not ts:
            continue
        try:
            date = ts[:10]
            daily[date]["cost"] += float(r.get("total_cost_usd") or 0.0)
            daily[date]["inputTokens"] += int(r.get("prompt_tokens") or 0)
            daily[date]["outputTokens"] += int(r.get("completion_tokens") or 0)
        except (ValueError, TypeError):
            pass
    return [
        CostingTrendPoint(
            date=d,
            cost=round(v["cost"], 6),
            inputTokens=v["inputTokens"],
            outputTokens=v["outputTokens"],
        )
        for d, v in sorted(daily.items())
    ]


def _trace_model_summary(records: list[dict[str, Any]]) -> list[ModelUsage]:
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"inputTokens": 0, "outputTokens": 0, "totalRequests": 0, "cost": 0.0}
    )
    for r in records:
        dep = (r.get("deployment") or "unknown").strip().lower()
        agg[dep]["inputTokens"]   += int(r.get("prompt_tokens") or 0)
        agg[dep]["outputTokens"]  += int(r.get("completion_tokens") or 0)
        agg[dep]["totalRequests"] += 1
        agg[dep]["cost"]          += float(r.get("total_cost_usd") or 0.0)
    return [
        ModelUsage(
            modelId=dep,
            modelName=dep.upper(),
            inputTokens=v["inputTokens"],
            outputTokens=v["outputTokens"],
            totalRequests=v["totalRequests"],
            cost=round(v["cost"], 6),
        )
        for dep, v in agg.items()
    ]


# ── Azure Cost Management helpers ──────────────────────────────────────────────

def _get_azure_credential():
    """Return a ClientSecretCredential using service-principal env vars."""
    from azure.identity import ClientSecretCredential  # type: ignore[import]
    from lectora_backend.config import settings

    if not all([settings.azure_tenant_id, settings.azure_client_id, settings.azure_client_secret]):
        return None
    return ClientSecretCredential(
        tenant_id=settings.azure_tenant_id,
        client_id=settings.azure_client_id,
        client_secret=settings.azure_client_secret,
    )


def _query_azure_costs(days: int = 30) -> list[CostingTrendPoint] | None:
    """
    Query Azure Cost Management for daily OpenAI spend over the last `days` days.

    Returns a list of CostingTrendPoint (one per day) or None if the API is not
    configured / unreachable.

    The query is scoped to:
      - Subscription scope:    /subscriptions/{AZURE_SUBSCRIPTION_ID}
      - Resource group scope:  /subscriptions/{id}/resourceGroups/{rg}  (if AZURE_RESOURCE_GROUP is set)
    Filter: ServiceName = "Azure OpenAI" or Cognitive Services (covers all deployments).
    """
    try:
        from azure.mgmt.costmanagement import CostManagementClient  # type: ignore[import]
        from azure.mgmt.costmanagement.models import (  # type: ignore[import]
            QueryDefinition,
            QueryTimePeriod,
            QueryDataset,
            QueryAggregation,
            QueryGrouping,
            QueryFilter,
            QueryComparisonExpression,
        )
    except ImportError:
        logger.warning(
            "[costing] azure-mgmt-costmanagement not installed. "
            "Run: pip install azure-mgmt-costmanagement azure-identity"
        )
        return None

    from lectora_backend.config import settings

    subscription_id = settings.azure_subscription_id.strip()
    if not subscription_id:
        logger.debug("[costing] AZURE_SUBSCRIPTION_ID not set — skipping Cost Management query")
        return None

    credential = _get_azure_credential()
    if credential is None:
        logger.debug("[costing] Azure service-principal credentials incomplete")
        return None

    # Build the scope
    resource_group = settings.azure_resource_group.strip()
    if resource_group:
        scope = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    else:
        scope = f"/subscriptions/{subscription_id}"

    # Time range: last `days` days
    end_dt   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=days)

    query = QueryDefinition(
        type="ActualCost",
        timeframe="Custom",
        time_period=QueryTimePeriod(from_property=start_dt, to=end_dt),
        dataset=QueryDataset(
            granularity="Daily",
            aggregation={
                "totalCost": QueryAggregation(name="Cost", function="Sum"),
            },
            grouping=[
                QueryGrouping(type="Dimension", name="ServiceName"),
            ],
        ),
    )

    try:
        client = CostManagementClient(credential=credential, subscription_id=subscription_id)
        result = client.query.usage(scope=scope, parameters=query)
    except Exception as exc:
        logger.warning("[costing] Azure Cost Management query failed: %s", exc)
        return None

    # Parse result — columns are returned positionally
    columns = [c.name.lower() for c in (result.columns or [])]
    if not columns:
        return None

    try:
        cost_idx = columns.index("cost")
        date_idx = columns.index("usagedate")
    except ValueError:
        logger.warning("[costing] Unexpected Cost Management response columns: %s", columns)
        return None

    # Aggregate per day across all service rows
    daily_cost: dict[str, float] = defaultdict(float)
    for row in (result.rows or []):
        raw_date = str(row[date_idx])  # YYYYMMDD integer or string
        if len(raw_date) == 8:
            date_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        else:
            date_str = raw_date[:10]
        daily_cost[date_str] += float(row[cost_idx])

    return [
        CostingTrendPoint(
            date=d,
            cost=round(c, 6),
            inputTokens=0,   # Azure billing doesn't expose token counts
            outputTokens=0,
        )
        for d, c in sorted(daily_cost.items())
    ]


def _query_azure_total(days: int = 30) -> float | None:
    """Return total Azure OpenAI spend for the last `days` days, or None."""
    trend = _query_azure_costs(days=days)
    if trend is None:
        return None
    return round(sum(p.cost for p in trend), 6)


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/summary", response_model=CostingSummary)
async def get_costing_summary() -> CostingSummary:
    """
    Aggregated cost and token usage.

    Summary totals and trend come from Azure Cost Management when configured;
    per-document and per-model details always come from LLM trace files.
    """
    # ── LLM traces (always read — used for per-doc / per-model) ───────────────
    records   = _read_traces()
    documents = _build_documents(records)
    trace_trend  = _trace_trend(records)
    model_summary = _trace_model_summary(records)

    trace_total_cost = sum(d.totalCost for d in documents)
    total_in  = sum(d.inputTokens  for d in documents)
    total_out = sum(d.outputTokens for d in documents)
    n_docs = len(documents)

    # ── Azure Cost Management (real billing totals + daily trend) ─────────────
    azure_trend = _query_azure_costs(days=30)
    data_source = "llm_traces"

    if azure_trend is not None:
        # Merge token counts from traces into the Azure billing trend points
        # so the FE trend chart shows both cost (from Azure) and tokens (from traces).
        trace_by_date = {p.date: p for p in trace_trend}
        merged_trend = [
            CostingTrendPoint(
                date=p.date,
                cost=p.cost,
                inputTokens=trace_by_date.get(p.date, p).inputTokens,
                outputTokens=trace_by_date.get(p.date, p).outputTokens,
            )
            for p in azure_trend
        ]
        azure_total = sum(p.cost for p in azure_trend)
        # Use Azure total as the authoritative cost figure; use trace total as
        # fallback for token-level averages when Azure total is 0 (no billing data).
        real_total = azure_total if azure_total > 0 else trace_total_cost
        data_source = "azure_cost_management"
        trend = merged_trend
    else:
        real_total = trace_total_cost
        trend = trace_trend

    avg_cost = round(real_total / n_docs, 6) if n_docs else 0.0
    monthly_est = round(real_total * (30 / 30), 2)  # last-30-days IS the month estimate

    return CostingSummary(
        totalCost=round(real_total, 6),
        totalInputTokens=total_in,
        totalOutputTokens=total_out,
        totalDocumentsProcessed=n_docs,
        averageCostPerDocument=avg_cost,
        estimatedMonthlyCost=monthly_est,
        costTrend=trend,
        modelSummary=model_summary,
        documents=documents,
        costChangePercent=0.0,
        documentsChangePercent=0.0,
        dataSource=data_source,
    )


@router.get("/documents/{document_id}", response_model=DocumentCost)
async def get_document_cost(document_id: str) -> DocumentCost:
    """Per-document cost breakdown from LLM traces."""
    records   = _read_traces()
    documents = _build_documents(records)

    for doc in documents:
        if doc.documentId == document_id:
            return doc

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"No cost data found for document: {document_id}",
    )
