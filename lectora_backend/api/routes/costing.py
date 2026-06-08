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
import time
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


class ServiceCostBreakdown(BaseModel):
    serviceName: str
    cost: float
    sharePercent: float


class AgentModelSummary(BaseModel):
    agentId: str
    agentName: str
    role: str
    pipelineStep: int
    stageKey: str
    stageName: str
    configuredDeployment: str
    configuredDeploymentLabel: str
    defaultDeployment: str
    isOverridden: bool
    totalRequests: int = 0
    inputTokens: int = 0
    outputTokens: int = 0
    cost: float = 0.0
    observedDeployments: list[str] = []


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
    stageSummary: list[StageBreakdown] = []
    serviceBreakdown: list[ServiceCostBreakdown] = []
    agentModelSummary: list[AgentModelSummary] = []
    traceTotalCost: float = 0.0
    azureTotalCost: float = 0.0
    costChangePercent: float | None = None
    documentsChangePercent: float | None = None
    dataSource: str  # "azure_cost_management" | "llm_traces" | "empty"
    currency: str = "USD"
    azureBillingConfigured: bool = False
    azureBillingError: str | None = None
    azureBillingSource: str | None = None  # service_principal | azure_cli | cache
    azureBillingStale: bool = False
    azureFetchedAt: str | None = None


# Set by the most recent Azure Cost Management query attempt (for FE error display).
_last_azure_billing_error: str | None = None

_AZURE_CACHE_FILE = _LOGS_ROOT.parent / ".azure_cost_cache.json"
_AZURE_CACHE_TTL_SECONDS = 30 * 60


# ── LLM Trace helpers ──────────────────────────────────────────────────────────

# Agents that call LLM but are not in model_registry (e.g. in-app editor ops).
_TRACE_ONLY_AGENT_META: dict[str, dict[str, Any]] = {
    "EDITOR": {
        "name": "Course Editor",
        "role": "In-app AI section operations (rewrite, tone, expand, simplify)",
        "pipeline_step": 4,
    },
}

_OPENAI_SERVICES = {
    "cognitive services",
    "azure openai",
    "azure openai service",
    "foundry models",
}


def _doc_from_trace_path(path: Path) -> str | None:
    """Infer doc_name from logs/{doc_name}/{agent}/llm_traces.jsonl layout."""
    try:
        rel = path.relative_to(_LOGS_ROOT)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 2 and parts[-1] == "llm_traces.jsonl":
        return parts[0]
    return None


def _doc_from_blob_path(blob_name: str) -> str | None:
    """Infer course/doc slug from Azure blob paths that contain trace files."""
    parts = [p for p in blob_name.split("/") if p]
    if not parts:
        return None
    if "logs" in parts:
        idx = parts.index("logs")
        if idx > 0:
            return parts[0]
    return parts[0]


def _trace_doc_key(record: dict[str, Any]) -> str:
    """Stable document key for grouping traces (doc_name → run_id → agent fallback)."""
    doc = (record.get("doc_name") or "").strip()
    if doc:
        return doc
    run_id = (record.get("run_id") or "").strip()
    if run_id:
        return f"run_{run_id}"
    agent = (record.get("agent") or "unknown").strip()
    ts = str(record.get("timestamp") or "")[:10]
    return f"untagged_{agent}_{ts}" if ts else f"untagged_{agent}"


def _format_document_name(doc_key: str) -> str:
    if doc_key in ("unknown", ""):
        return "Untagged AI Operations"
    if doc_key.startswith("run_"):
        return f"Pipeline Run {doc_key[4:12]}"
    if doc_key.startswith("untagged_"):
        return doc_key.replace("untagged_", "Untagged ").replace("_", " ")
    return doc_key.replace("_", " ")


def _enrich_trace_record(
    record: dict[str, Any],
    *,
    path_hint: str | None = None,
) -> dict[str, Any]:
    if (record.get("doc_name") or "").strip():
        return record
    if path_hint:
        return {**record, "doc_name": path_hint}
    return record


def _parse_trace_jsonl(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def _read_traces_from_local() -> list[dict[str, Any]]:
    """Read llm_traces.jsonl files from the local pipeline/logs/ tree."""
    records: list[dict[str, Any]] = []
    if not _LOGS_ROOT.exists():
        return records
    for trace_file in _LOGS_ROOT.rglob("llm_traces.jsonl"):
        path_doc = _doc_from_trace_path(trace_file)
        try:
            for record in _parse_trace_jsonl(trace_file.read_text(encoding="utf-8")):
                records.append(_enrich_trace_record(record, path_hint=path_doc))
        except OSError:
            pass
    return records


def _read_traces_from_azure() -> list[dict[str, Any]]:
    """Read llm_traces.jsonl blobs synced to Azure (pipeline + course artifacts)."""
    from lectora_backend.config import settings

    if not settings.is_azure_storage_configured():
        return []

    from lectora_backend.repositories.blob_repository import BlobRepository

    containers = {
        settings.blob_container_name,
        settings.course_generation_artifacts_container_name,
    }
    records: list[dict[str, Any]] = []
    for container in containers:
        if not container:
            continue
        try:
            repo = BlobRepository(container_name=container)
            for blob_name in repo.list_blobs(""):
                if not blob_name.endswith("llm_traces.jsonl"):
                    continue
                try:
                    path_doc = _doc_from_blob_path(blob_name)
                    for record in _parse_trace_jsonl(repo.download_text(blob_name)):
                        records.append(_enrich_trace_record(record, path_hint=path_doc))
                except Exception as exc:
                    logger.debug(
                        "[costing] Failed to read trace blob %s/%s: %s",
                        container,
                        blob_name,
                        exc,
                    )
        except Exception as exc:
            logger.debug("[costing] Azure trace scan failed for %s: %s", container, exc)
    return records


def _read_traces() -> list[dict[str, Any]]:
    """Merge local and Azure LLM trace files (deduped by timestamp + agent + doc)."""
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for record in _read_traces_from_local() + _read_traces_from_azure():
        key = "|".join(
            [
                str(record.get("timestamp") or ""),
                str(record.get("doc_name") or ""),
                str(record.get("agent") or ""),
                str(record.get("deployment") or ""),
                str(record.get("prompt_tokens") or ""),
                str(record.get("completion_tokens") or ""),
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(record)
    return merged


def _build_documents(records: list[dict[str, Any]]) -> list[DocumentCost]:
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_doc[_trace_doc_key(r)].append(r)

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

        doc_type = "Course" if not doc_name.startswith(("untagged_", "run_")) and doc_name != "unknown" else "Trace Run"

        docs.append(DocumentCost(
            documentId=doc_name,
            documentName=_format_document_name(doc_name),
            documentType=doc_type,
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
                    modelName=_deployment_label(dep),
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


def _parse_trace_timestamp(ts: str) -> datetime | None:
    """Parse an ISO or YYYY-MM-DD trace timestamp to UTC."""
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        if len(ts) >= 10:
            try:
                return datetime.strptime(ts[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        return None


def _percent_change(current: float, previous: float) -> float | None:
    """Return period-over-period % change, or None when comparison is not meaningful."""
    if previous <= 0:
        return 100.0 if current > 0 else None
    return round(((current - previous) / previous) * 100, 1)


def _trace_period_metrics(
    records: list[dict[str, Any]],
    *,
    period_days: int = 30,
) -> tuple[float, float, int, int]:
    """
    Aggregate trace cost and unique documents for the most recent `period_days`
    versus the prior `period_days`.
    """
    now = datetime.now(timezone.utc)
    current_start = now - timedelta(days=period_days)
    previous_start = now - timedelta(days=period_days * 2)

    current_cost = previous_cost = 0.0
    current_docs: set[str] = set()
    previous_docs: set[str] = set()

    for record in records:
        ts = _parse_trace_timestamp(str(record.get("timestamp") or ""))
        if ts is None:
            continue
        cost = float(record.get("total_cost_usd") or 0.0)
        doc_key = _trace_doc_key(record)
        if current_start <= ts <= now:
            current_cost += cost
            current_docs.add(doc_key)
        elif previous_start <= ts < current_start:
            previous_cost += cost
            previous_docs.add(doc_key)

    return current_cost, previous_cost, len(current_docs), len(previous_docs)


def _trend_period_costs(
    trend: list[CostingTrendPoint],
    *,
    period_days: int = 30,
) -> tuple[float, float]:
    """Sum daily trend costs for the latest vs prior 30-day windows."""
    if not trend:
        return 0.0, 0.0
    now = datetime.now(timezone.utc)
    current_start = (now - timedelta(days=period_days)).date()
    previous_start = (now - timedelta(days=period_days * 2)).date()

    current_cost = previous_cost = 0.0
    for point in trend:
        try:
            point_date = datetime.strptime(point.date[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if point_date >= current_start:
            current_cost += point.cost
        elif previous_start <= point_date < current_start:
            previous_cost += point.cost
    return current_cost, previous_cost


def _deployment_label(deployment_id: str) -> str:
    from lectora_backend.pipeline.shared_llm_config.model_registry import AVAILABLE_MODELS

    dep = deployment_id.strip().lower()
    for model in AVAILABLE_MODELS:
        if model["id"].lower() == dep:
            return model["label"]
    return deployment_id


def _normalize_trace_agent(agent: str) -> str:
    return (agent or "UNKNOWN").strip().upper()


def _build_agent_model_summary(records: list[dict[str, Any]]) -> list[AgentModelSummary]:
    """Merge model_registry configs with per-agent trace usage."""
    from lectora_backend.pipeline.shared_llm_config.model_registry import get_all_configs

    trace_by_agent: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "inputTokens": 0,
            "outputTokens": 0,
            "totalRequests": 0,
            "cost": 0.0,
            "deployments": set(),
        }
    )
    for record in records:
        agent_id = _normalize_trace_agent(str(record.get("agent") or ""))
        dep = (record.get("deployment") or "").strip().lower()
        trace_by_agent[agent_id]["inputTokens"] += int(record.get("prompt_tokens") or 0)
        trace_by_agent[agent_id]["outputTokens"] += int(record.get("completion_tokens") or 0)
        trace_by_agent[agent_id]["totalRequests"] += 1
        trace_by_agent[agent_id]["cost"] += float(record.get("total_cost_usd") or 0.0)
        if dep:
            trace_by_agent[agent_id]["deployments"].add(dep)

    summaries: list[AgentModelSummary] = []
    registry_ids: set[str] = set()

    for cfg in sorted(get_all_configs(), key=lambda c: c["pipeline_step"]):
        agent_id = cfg["agent_id"]
        registry_ids.add(agent_id)
        trace = trace_by_agent.get(agent_id, {})
        stage_key = _AGENT_TO_STAGE.get(agent_id, "other")
        deployment = cfg["current_deployment"]
        summaries.append(
            AgentModelSummary(
                agentId=agent_id,
                agentName=cfg["name"],
                role=cfg["role"],
                pipelineStep=cfg["pipeline_step"],
                stageKey=stage_key,
                stageName=_STAGE_NAMES.get(stage_key, stage_key),
                configuredDeployment=deployment,
                configuredDeploymentLabel=_deployment_label(deployment),
                defaultDeployment=cfg["default_deployment"],
                isOverridden=cfg["is_overridden"],
                totalRequests=int(trace.get("totalRequests") or 0),
                inputTokens=int(trace.get("inputTokens") or 0),
                outputTokens=int(trace.get("outputTokens") or 0),
                cost=round(float(trace.get("cost") or 0.0), 6),
                observedDeployments=sorted(trace.get("deployments") or []),
            )
        )

    for agent_id, trace in trace_by_agent.items():
        if agent_id in registry_ids or int(trace.get("totalRequests") or 0) == 0:
            continue
        meta = _TRACE_ONLY_AGENT_META.get(
            agent_id,
            {
                "name": agent_id.title(),
                "role": "Additional LLM operations",
                "pipeline_step": 99,
            },
        )
        stage_key = _AGENT_TO_STAGE.get(agent_id, "other")
        observed = sorted(trace.get("deployments") or [])
        deployment = observed[0] if observed else "unknown"
        summaries.append(
            AgentModelSummary(
                agentId=agent_id,
                agentName=meta["name"],
                role=meta["role"],
                pipelineStep=meta["pipeline_step"],
                stageKey=stage_key,
                stageName=_STAGE_NAMES.get(stage_key, stage_key),
                configuredDeployment=deployment,
                configuredDeploymentLabel=_deployment_label(deployment),
                defaultDeployment=deployment,
                isOverridden=False,
                totalRequests=int(trace.get("totalRequests") or 0),
                inputTokens=int(trace.get("inputTokens") or 0),
                outputTokens=int(trace.get("outputTokens") or 0),
                cost=round(float(trace.get("cost") or 0.0), 6),
                observedDeployments=observed,
            )
        )

    return sorted(summaries, key=lambda s: (s.pipelineStep, s.agentId))


def _build_stage_summary(records: list[dict[str, Any]]) -> list[StageBreakdown]:
    stage_agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"inputTokens": 0, "outputTokens": 0, "cost": 0.0, "requests": 0}
    )
    for r in records:
        agent = (r.get("agent") or "").upper()
        stage_key = _AGENT_TO_STAGE.get(agent, "other")
        stage_agg[stage_key]["inputTokens"] += int(r.get("prompt_tokens") or 0)
        stage_agg[stage_key]["outputTokens"] += int(r.get("completion_tokens") or 0)
        stage_agg[stage_key]["cost"] += float(r.get("total_cost_usd") or 0.0)
        stage_agg[stage_key]["requests"] += 1
    return sorted(
        [
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
        key=lambda s: s.cost,
        reverse=True,
    )


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
    return sorted(
        [
            ModelUsage(
                modelId=dep,
                modelName=_deployment_label(dep),
                inputTokens=v["inputTokens"],
                outputTokens=v["outputTokens"],
                totalRequests=v["totalRequests"],
                cost=round(v["cost"], 6),
            )
            for dep, v in agg.items()
        ],
        key=lambda m: m.cost,
        reverse=True,
    )


# ── Azure Cost Management helpers ──────────────────────────────────────────────

def _get_service_principal_credential():
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


def _cost_management_credentials() -> list[tuple[str, object]]:
    """
    Credentials to try for Cost Management queries, in priority order.

    1. Service principal (production)
    2. Azure CLI session (local dev fallback when ``az login`` has access but SP does not)
    """
    creds: list[tuple[str, object]] = []
    sp = _get_service_principal_credential()
    if sp is not None:
        creds.append(("service_principal", sp))
    try:
        from azure.identity import AzureCliCredential  # type: ignore[import]

        creds.append(("azure_cli", AzureCliCredential()))
    except Exception:
        pass
    return creds


def _azure_billing_is_configured() -> bool:
    from lectora_backend.config import settings

    return bool(
        settings.azure_subscription_id.strip()
        and _cost_management_credentials()
    )


def _is_authorization_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "authorizationfailed" in text or "does not have authorization" in text


def _is_rate_limit_error(exc: Exception) -> bool:
    return "429" in str(exc) or "too many requests" in str(exc).lower()


def _find_column_index(columns: list[str], *candidates: str) -> int:
    """Return index of the first matching column name (case-insensitive)."""
    for cand in candidates:
        if cand in columns:
            return columns.index(cand)
    for i, col in enumerate(columns):
        if "costusd" in col or col in ("cost", "pretaxcost", "totalcost", "totalcostusd"):
            return i
    raise ValueError(f"Azure Cost Management: no cost column in {columns}")


def _load_azure_cost_cache() -> dict[str, Any] | None:
    if not _AZURE_CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(_AZURE_CACHE_FILE.read_text(encoding="utf-8"))
        fetched_at = _parse_trace_timestamp(str(payload.get("fetched_at") or ""))
        if fetched_at is None:
            return None
        age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if age > _AZURE_CACHE_TTL_SECONDS:
            return None
        return payload
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _load_stale_azure_cost_cache() -> dict[str, Any] | None:
    """Return cache regardless of TTL — used when live API is rate-limited."""
    if not _AZURE_CACHE_FILE.exists():
        return None
    try:
        return json.loads(_AZURE_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_azure_cost_cache(
    *,
    days: int,
    source: str,
    trend: list[CostingTrendPoint],
    services: list[ServiceCostBreakdown],
) -> None:
    try:
        _AZURE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _AZURE_CACHE_FILE.write_text(
            json.dumps(
                {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "source": source,
                    "days": days,
                    "trend": [p.model_dump() for p in trend],
                    "services": [s.model_dump() for s in services],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("[costing] Failed to write Azure cost cache: %s", exc)


def _cache_to_azure_result(
    payload: dict[str, Any],
    *,
    stale: bool,
) -> tuple[list[CostingTrendPoint], list[ServiceCostBreakdown], str, str | None]:
    trend = [
        CostingTrendPoint(**row)
        for row in (payload.get("trend") or [])
    ]
    services = [
        ServiceCostBreakdown(**row)
        for row in (payload.get("services") or [])
    ]
    source = str(payload.get("source") or "cache")
    fetched_at = str(payload.get("fetched_at") or "") or None
    if stale:
        source = f"{source}+cache"
    return trend, services, source, fetched_at


def _parse_cost_management_result(
    result: object,
) -> tuple[list[CostingTrendPoint], list[ServiceCostBreakdown]]:
    columns = [c.name.lower() for c in (result.columns or [])]  # type: ignore[attr-defined]
    if not columns:
        raise ValueError("Azure Cost Management returned no columns")

    cost_idx = _find_column_index(
        columns, "costusd", "totalcostusd", "cost", "pretaxcost", "totalcost",
    )
    try:
        date_idx = _find_column_index(
            columns, "usagedate", "date", "billingperiodstartdate",
        )
    except ValueError as exc:
        raise ValueError(f"Azure Cost Management: no date column in {columns}") from exc

    service_idx = columns.index("servicename") if "servicename" in columns else None

    daily_cost: dict[str, float] = defaultdict(float)
    service_cost: dict[str, float] = defaultdict(float)
    for row in (result.rows or []):  # type: ignore[attr-defined]
        service_label = "Azure OpenAI"
        if service_idx is not None:
            service_name = str(row[service_idx]).strip()
            if service_name.lower() not in _OPENAI_SERVICES:
                continue
            service_label = service_name
        raw_date = str(row[date_idx])
        if len(raw_date) == 8 and raw_date.isdigit():
            date_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        else:
            date_str = raw_date[:10]
        amount = float(row[cost_idx])
        daily_cost[date_str] += amount
        service_cost[service_label] += amount

    trend = [
        CostingTrendPoint(
            date=d,
            cost=round(c, 6),
            inputTokens=0,
            outputTokens=0,
        )
        for d, c in sorted(daily_cost.items())
    ]
    service_total = sum(service_cost.values())
    services = [
        ServiceCostBreakdown(
            serviceName=name,
            cost=round(cost, 6),
            sharePercent=round((cost / service_total) * 100, 1) if service_total else 0.0,
        )
        for name, cost in sorted(service_cost.items(), key=lambda item: -item[1])
    ]
    return trend, services


class _AzureCostResult(BaseModel):
    trend: list[CostingTrendPoint]
    services: list[ServiceCostBreakdown]
    source: str
    fetched_at: str | None = None
    stale: bool = False


def _query_azure_costs(days: int = 30) -> _AzureCostResult | None:
    """
    Query Azure Cost Management for daily OpenAI spend over the last `days` days.

    Uses a 30-minute file cache and falls back to stale cache on 429 rate limits.
    Credential order: azure_cli first (local dev), then service principal (production).
    """
    global _last_azure_billing_error
    _last_azure_billing_error = None

    cached = _load_azure_cost_cache()
    if cached and int(cached.get("days") or 0) >= days:
        trend, services, source, fetched_at = _cache_to_azure_result(cached, stale=False)
        if trend:
            logger.info("[costing] Azure Cost Management cache hit (%d days)", days)
            return _AzureCostResult(
                trend=trend, services=services, source=source, fetched_at=fetched_at,
            )

    try:
        from azure.mgmt.costmanagement import CostManagementClient  # type: ignore[import]
        from azure.mgmt.costmanagement.models import (  # type: ignore[import]
            QueryDefinition,
            QueryTimePeriod,
            QueryDataset,
            QueryAggregation,
            QueryGrouping,
        )
    except ImportError:
        logger.warning(
            "[costing] azure-mgmt-costmanagement not installed. "
            "Run: pip install azure-mgmt-costmanagement azure-identity"
        )
        return _fallback_stale_azure_cache(days)

    from lectora_backend.config import settings

    subscription_id = settings.azure_subscription_id.strip()
    if not subscription_id:
        logger.debug("[costing] AZURE_SUBSCRIPTION_ID not set — skipping Cost Management query")
        return _fallback_stale_azure_cache(days)

    credentials = _cost_management_credentials()
    # Prefer CLI in local dev — service principal often lacks Cost Management Reader.
    credentials = sorted(credentials, key=lambda item: 0 if item[0] == "azure_cli" else 1)
    if not credentials:
        logger.debug("[costing] No Azure credentials available for Cost Management")
        return _fallback_stale_azure_cache(days)

    resource_group = settings.azure_resource_group.strip()
    scope = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        if resource_group
        else f"/subscriptions/{subscription_id}"
    )

    end_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=days)

    query = QueryDefinition(
        type="ActualCost",
        timeframe="Custom",
        time_period=QueryTimePeriod(from_property=start_dt, to=end_dt),
        dataset=QueryDataset(
            granularity="Daily",
            aggregation={
                "totalCostUsd": QueryAggregation(name="CostUSD", function="Sum"),
            },
            grouping=[
                QueryGrouping(type="Dimension", name="ServiceName"),
            ],
        ),
    )

    errors: list[str] = []
    saw_rate_limit = False
    for label, credential in credentials:
        max_attempts = 4 if label == "azure_cli" else 2
        for attempt in range(max_attempts):
            try:
                client = CostManagementClient(
                    credential=credential, subscription_id=subscription_id,
                )
                result = client.query.usage(scope=scope, parameters=query)
                trend, services = _parse_cost_management_result(result)
                fetched_at = datetime.now(timezone.utc).isoformat()
                _save_azure_cost_cache(
                    days=days, source=label, trend=trend, services=services,
                )
                logger.info(
                    "[costing] Azure Cost Management OK via %s (%d days, %d USD points, %d services)",
                    label,
                    days,
                    len(trend),
                    len(services),
                )
                return _AzureCostResult(
                    trend=trend,
                    services=services,
                    source=label,
                    fetched_at=fetched_at,
                )
            except Exception as exc:
                err_text = str(exc).strip() or type(exc).__name__
                if _is_rate_limit_error(exc):
                    saw_rate_limit = True
                    if attempt < max_attempts - 1:
                        time.sleep(min(30, 5 * (2 ** attempt)))
                        continue
                msg = f"{label}: {err_text}"
                errors.append(msg)
                if _is_authorization_error(exc):
                    logger.debug("[costing] Cost Management auth failed for %s", label)
                    break
                logger.warning(
                    "[costing] Azure Cost Management query failed (%s): %s", label, err_text,
                )
                break

    stale = _fallback_stale_azure_cache(days)
    if stale is not None:
        if saw_rate_limit:
            _last_azure_billing_error = (
                "Azure Cost Management rate-limited (429) — showing cached billing data. "
                "Refresh again in a few minutes."
            )
        return stale

    _last_azure_billing_error = errors[-1] if errors else "Azure Cost Management query failed"
    return None


def _fallback_stale_azure_cache(days: int) -> _AzureCostResult | None:
    payload = _load_stale_azure_cost_cache()
    if not payload:
        return None
    trend, services, source, fetched_at = _cache_to_azure_result(payload, stale=True)
    if not trend:
        return None
    logger.info("[costing] Using stale Azure cost cache (%d trend points)", len(trend))
    return _AzureCostResult(
        trend=trend, services=services, source=source, fetched_at=fetched_at, stale=True,
    )


def _query_azure_total(days: int = 30) -> float | None:
    """Return total Azure OpenAI spend for the last `days` days, or None."""
    result = _query_azure_costs(days=days)
    if result is None:
        return None
    return round(sum(p.cost for p in result.trend), 6)


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
    stage_summary = _build_stage_summary(records)
    agent_model_summary = _build_agent_model_summary(records)

    trace_total_cost = round(sum(d.totalCost for d in documents), 6)
    total_in  = sum(d.inputTokens  for d in documents)
    total_out = sum(d.outputTokens for d in documents)
    n_docs = len(documents)

    # ── Azure Cost Management (cached; 60-day window for period comparisons) ───
    azure_result = _query_azure_costs(days=60)
    data_source = "llm_traces"
    azure_billing_ok = azure_result is not None
    service_breakdown: list[ServiceCostBreakdown] = []
    azure_total = 0.0
    azure_billing_source: str | None = None
    azure_billing_stale = False
    azure_fetched_at: str | None = None

    if azure_billing_ok:
        azure_60 = azure_result.trend
        azure_services_60 = azure_result.services
        azure_billing_source = azure_result.source
        azure_billing_stale = azure_result.stale
        azure_fetched_at = azure_result.fetched_at
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date()
        azure_trend_30 = [
            p for p in azure_60
            if datetime.strptime(p.date[:10], "%Y-%m-%d").date() >= cutoff
        ]
        trace_by_date = {p.date: p for p in trace_trend}
        merged_trend = [
            CostingTrendPoint(
                date=p.date,
                cost=p.cost,
                inputTokens=trace_by_date.get(p.date, p).inputTokens,
                outputTokens=trace_by_date.get(p.date, p).outputTokens,
            )
            for p in azure_trend_30
        ]
        azure_total = round(sum(p.cost for p in azure_trend_30), 6)
        azure_total_60 = sum(p.cost for p in azure_60)
        if azure_total_60 > 0 and azure_services_60:
            scale = azure_total / azure_total_60
            scaled = [
                (s.serviceName, s.cost * scale)
                for s in azure_services_60
            ]
            scaled_total = sum(cost for _, cost in scaled)
            service_breakdown = [
                ServiceCostBreakdown(
                    serviceName=name,
                    cost=round(cost, 6),
                    sharePercent=round((cost / scaled_total) * 100, 1) if scaled_total else 0.0,
                )
                for name, cost in scaled
            ]
        real_total = azure_total if azure_total > 0 else trace_total_cost
        data_source = "azure_cost_management"
        trend = merged_trend
        cur_cost, prev_cost = _trend_period_costs(azure_60)
        cost_change = _percent_change(cur_cost, prev_cost)
    else:
        real_total = trace_total_cost
        trend = trace_trend
        cur_cost, prev_cost, _, _ = _trace_period_metrics(records)
        cost_change = _percent_change(cur_cost, prev_cost)

    avg_cost = round(real_total / n_docs, 6) if n_docs else 0.0
    monthly_est = round(real_total, 2)

    _, _, cur_docs, prev_docs = _trace_period_metrics(records)
    documents_change = _percent_change(float(cur_docs), float(prev_docs))

    azure_configured = _azure_billing_is_configured()
    if not azure_billing_ok and not trend and not documents and real_total == 0:
        data_source = "empty"

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
        stageSummary=stage_summary,
        serviceBreakdown=service_breakdown,
        agentModelSummary=agent_model_summary,
        traceTotalCost=trace_total_cost,
        azureTotalCost=azure_total,
        costChangePercent=cost_change,
        documentsChangePercent=documents_change,
        dataSource=data_source,
        currency="USD",
        azureBillingConfigured=azure_configured,
        azureBillingError=(
            _last_azure_billing_error
            if azure_configured and not azure_billing_ok
            else (_last_azure_billing_error if azure_billing_stale else None)
        ),
        azureBillingSource=azure_billing_source,
        azureBillingStale=azure_billing_stale,
        azureFetchedAt=azure_fetched_at,
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
