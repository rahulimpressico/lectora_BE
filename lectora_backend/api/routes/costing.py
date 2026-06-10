"""
GET /costing/summary
GET /costing/documents/{documentId}

All cost and token data comes from LLM trace files (``llm_traces.jsonl``) written
by tracer.py — read from local ``pipeline/logs/`` and, when configured, synced
Azure Blob artifacts. Returns empty collections when no traces exist.
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

# ── Agent → stage key mapping (names match model_registry + pipeline agents) ───
_AGENT_TO_STAGE: dict[str, str] = {
    "A0":             "a0_classification",
    "A0_TO":          "to_generation",
    "A1":             "outline_interpretation",
    "S1":             "structure_review",
    "SECTION_MAPPER": "section_mapping",
    "KC_PLANNER":     "kc_planning",
    "A2":             "content_generation",
    "S2":             "quality_assurance",
    "EDITOR":         "course_editor",
}
_STAGE_NAMES: dict[str, str] = {
    "a0_classification":     "Rule Classification",
    "to_generation":         "TO Generation",
    "outline_interpretation": "Outline Interpretation",
    "structure_review":      "Structure Review",
    "section_mapping":       "Section Mapping",
    "kc_planning":           "KC Planning",
    "content_generation":    "Content Generation",
    "quality_assurance":     "Quality Assurance",
    "course_editor":         "Course Editor",
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
    runSummary: str
    status: str
    totalCost: float
    inputTokens: int
    outputTokens: int
    totalRequests: int
    modelsUsed: list[str]
    agentsUsed: list[str] = []
    lastUpdated: str
    modelBreakdown: list[ModelUsage]
    stageBreakdown: list[StageBreakdown]


class CostingTrendPoint(BaseModel):
    date: str
    cost: float
    inputTokens: int
    outputTokens: int


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
    agentModelSummary: list[AgentModelSummary] = []
    traceTotalCost: float = 0.0
    costChangePercent: float | None = None
    documentsChangePercent: float | None = None
    dataSource: str  # "llm_traces" | "empty"
    currency: str = "USD"


# ── LLM Trace helpers ──────────────────────────────────────────────────────────

# Agents that call LLM but are not in model_registry (e.g. in-app editor ops).
_TRACE_ONLY_AGENT_META: dict[str, dict[str, Any]] = {
    "EDITOR": {
        "name": "Course Editor",
        "role": "In-app AI section operations (rewrite, tone, expand, simplify)",
        "pipeline_step": 4,
    },
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


def _canonical_doc_key(doc_key: str) -> str:
    """Normalize doc keys so the same source file groups together in costing."""
    return doc_key.strip().lower().replace(" ", "_")


def _enrich_records_doc_names(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill missing doc_name from sibling traces that share the same run_id."""
    run_to_doc: dict[str, str] = {}
    for record in records:
        doc = (record.get("doc_name") or "").strip()
        run_id = (record.get("run_id") or "").strip()
        if doc and run_id:
            run_to_doc[run_id] = doc

    enriched: list[dict[str, Any]] = []
    for record in records:
        if (record.get("doc_name") or "").strip():
            enriched.append(record)
            continue
        run_id = (record.get("run_id") or "").strip()
        if run_id and run_id in run_to_doc:
            enriched.append({**record, "doc_name": run_to_doc[run_id]})
            continue
        enriched.append(record)
    return enriched


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


def _agents_in_traces(traces: list[dict[str, Any]]) -> set[str]:
    return {
        str(t.get("agent") or "").strip().upper()
        for t in traces
        if str(t.get("agent") or "").strip()
    }


def _infer_document_type(doc_key: str, agents: set[str]) -> str:
    if doc_key.startswith("run_"):
        return "Pipeline Run"
    if doc_key.startswith("untagged_"):
        return "Untagged Run"
    if "A2" in agents:
        return "Course Generation"
    if agents <= {"A0", "A0_TO"}:
        return "TO Generation"
    if "A1" in agents:
        return "Outline Processing"
    if "EDITOR" in agents:
        return "Course Editor"
    return "Document Run"


def _document_run_summary(doc_key: str, agents: set[str]) -> str:
    if doc_key.startswith("run_"):
        return (
            "All LLM calls from one pipeline job, grouped by run ID. "
            "Includes every traced agent that executed in that run."
        )
    if doc_key.startswith("untagged_"):
        return "LLM calls that were not tagged with a course or document name in tracer.py."
    if "A2" in agents:
        parts = []
        if "A0" in agents or "A0_TO" in agents:
            parts.append("TO/classification")
        if "A1" in agents:
            parts.append("outline interpretation")
        parts.append("lesson content generation")
        if "EDITOR" in agents:
            parts.append("in-app editor AI")
        return (
            f"Full course workflow for this document: {', then '.join(parts)}. "
            "Cost is the sum of every traced LLM request for this slug."
        )
    if agents <= {"A0", "A0_TO"}:
        return (
            "Timed Outline or rule-classification work only — no A1 outline parsing "
            "or A2 content generation in these traces."
        )
    if "A1" in agents:
        return (
            "Outline interpretation and course-structure work (A1). "
            "No A2 lesson content generation in these traces."
        )
    if "EDITOR" in agents:
        return "In-app course editor AI operations (rewrite, expand, tone, simplify)."
    return "LLM usage grouped by the document slug recorded in tracer.py."


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
    raw_records = _enrich_records_doc_names(
        _read_traces_from_local() + _read_traces_from_azure()
    )
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for record in raw_records:
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


def _merge_document_trace_groups(
    by_doc: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """
    Merge trace groups that refer to the same source document under different keys.

    Handles case/format drift (``Long_Term_Care`` vs ``long_term_care``) and rolls
    TO-only runs (A0/A0_TO) into the matching full-course document when they share
    the same canonical doc key.
    """
    canonical: dict[str, str] = {}
    merged: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # Prefer the doc key that already has full-pipeline agents (A1/A2).
    def _rank(key: str, traces: list[dict[str, Any]]) -> tuple[int, int]:
        agents = _agents_in_traces(traces)
        full_pipeline = 2 if "A2" in agents else 1 if "A1" in agents else 0
        return (full_pipeline, len(traces))

    for doc_key, traces in by_doc.items():
        canon = _canonical_doc_key(doc_key)
        if canon not in canonical:
            canonical[canon] = doc_key
            merged[doc_key].extend(traces)
            continue
        existing_key = canonical[canon]
        existing_traces = merged[existing_key]
        if _rank(doc_key, traces) > _rank(existing_key, existing_traces):
            merged[doc_key] = existing_traces + traces
            del merged[existing_key]
            canonical[canon] = doc_key
        else:
            merged[existing_key].extend(traces)

    return dict(merged)


def _build_documents(records: list[dict[str, Any]]) -> list[DocumentCost]:
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_doc[_trace_doc_key(r)].append(r)
    by_doc = _merge_document_trace_groups(by_doc)

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

        agents_used = _agents_in_traces(traces)
        doc_type = _infer_document_type(doc_name, agents_used)
        run_summary = _document_run_summary(doc_name, agents_used)

        model_breakdown = sorted(
            [
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
            key=lambda m: m.cost,
            reverse=True,
        )
        stage_breakdown = sorted(
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

        docs.append(DocumentCost(
            documentId=doc_name,
            documentName=_format_document_name(doc_name),
            documentType=doc_type,
            runSummary=run_summary,
            status="completed",
            totalCost=round(total_cost, 6),
            inputTokens=total_in,
            outputTokens=total_out,
            totalRequests=total_req,
            modelsUsed=[m.modelId for m in model_breakdown],
            agentsUsed=sorted(agents_used),
            lastUpdated=last_ts or datetime.now(timezone.utc).isoformat(),
            modelBreakdown=model_breakdown,
            stageBreakdown=stage_breakdown,
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


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/summary", response_model=CostingSummary)
async def get_costing_summary() -> CostingSummary:
    """Aggregated cost and token usage from LLM trace files."""
    records = _read_traces()
    documents = _build_documents(records)
    trace_trend = _trace_trend(records)
    model_summary = _trace_model_summary(records)
    stage_summary = _build_stage_summary(records)
    agent_model_summary = _build_agent_model_summary(records)

    trace_total_cost = round(sum(d.totalCost for d in documents), 6)
    total_in = sum(d.inputTokens for d in documents)
    total_out = sum(d.outputTokens for d in documents)
    n_docs = len(documents)

    cur_cost, prev_cost, cur_docs, prev_docs = _trace_period_metrics(records)
    cost_change = _percent_change(cur_cost, prev_cost)
    documents_change = _percent_change(float(cur_docs), float(prev_docs))

    data_source = "llm_traces" if (trace_trend or documents or trace_total_cost > 0) else "empty"
    avg_cost = round(trace_total_cost / n_docs, 6) if n_docs else 0.0

    return CostingSummary(
        totalCost=trace_total_cost,
        totalInputTokens=total_in,
        totalOutputTokens=total_out,
        totalDocumentsProcessed=n_docs,
        averageCostPerDocument=avg_cost,
        estimatedMonthlyCost=round(trace_total_cost, 2),
        costTrend=trace_trend,
        modelSummary=model_summary,
        documents=documents,
        stageSummary=stage_summary,
        agentModelSummary=agent_model_summary,
        traceTotalCost=trace_total_cost,
        costChangePercent=cost_change,
        documentsChangePercent=documents_change,
        dataSource=data_source,
        currency="USD",
    )


@router.get("/documents/{document_id}", response_model=DocumentCost)
async def get_document_cost(document_id: str) -> DocumentCost:
    """Per-document cost breakdown from LLM traces."""
    records = _read_traces()
    documents = _build_documents(records)

    for doc in documents:
        if doc.documentId == document_id:
            return doc

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"No cost data found for document: {document_id}",
    )
