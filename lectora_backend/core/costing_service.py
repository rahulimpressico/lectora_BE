from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lectora_backend.api.schemas.costing_schemas import (
    CostingSummaryResponse,
    CostingTrendPointResponse,
    DocumentCostResponse,
    ModelUsageResponse,
    StageBreakdownResponse,
)


TRACE_ROOT = Path(__file__).resolve().parents[1] / "pipeline" / "logs"
SHARED_STATE_ROOT = Path(__file__).resolve().parents[1] / "pipeline" / "shared_state"

MODEL_NAMES = {
    "o3": "O3",
    "gpt-5.4": "GPT-5.4",
    "gpt-5.4-mini": "GPT-5.4 Mini",
    "gpt-5-mini": "GPT-5 Mini",
}

AGENT_STAGE_MAP = {
    "A0": ("to_generation", "TO Generation"),
    "A1": ("metadata_generation", "Metadata Generation"),
    "A2": ("content_generation", "Content Generation"),
    "S1": ("assessment_generation", "Assessment Generation"),
    "S2": ("assessment_generation", "Assessment Generation"),
}


@dataclass
class TraceRecord:
    run_id: str
    doc_name: str
    agent: str
    deployment: str
    timestamp: datetime
    prompt_tokens: int
    completion_tokens: int
    total_cost_usd: float
    search_text: str


@dataclass
class RunMetadata:
    document_id: str
    document_name: str
    document_type: str
    status: str
    run_timestamp: datetime | None
    raw: dict[str, Any]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _round_cost(value: float) -> float:
    return round(value, 6)


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _derive_status(shared_dir: Path) -> str:
    if (shared_dir / "generated_content.json").exists():
        return "completed"
    if (shared_dir / "s2_validation.json").exists():
        s2 = _safe_read_json(shared_dir / "s2_validation.json") or {}
        status = str(s2.get("status", "")).lower()
        if "block" in status or "fail" in status:
            return "failed"
    if (shared_dir / "s1_validation.json").exists():
        s1 = _safe_read_json(shared_dir / "s1_validation.json") or {}
        status = str(s1.get("status", "")).lower()
        if "block" in status or "fail" in status:
            return "failed"
    return "in-progress"


def _load_run_metadata() -> dict[str, RunMetadata]:
    runs: dict[str, RunMetadata] = {}
    if not SHARED_STATE_ROOT.exists():
        return runs

    for shared_dir in sorted(p for p in SHARED_STATE_ROOT.iterdir() if p.is_dir()):
        request_spec = _safe_read_json(shared_dir / "request_spec.json")
        if not request_spec:
            continue

        run_id = str(request_spec.get("run_id") or "").strip()
        if not run_id:
            continue

        course_metadata = request_spec.get("course_metadata") or {}
        document_name = str(course_metadata.get("title") or shared_dir.name)
        document_type = str(
            course_metadata.get("category")
            or course_metadata.get("course_type")
            or request_spec.get("rule_classification", {}).get("family")
            or "Unknown"
        )
        runs[run_id] = RunMetadata(
            document_id=run_id,
            document_name=document_name,
            document_type=document_type,
            status=_derive_status(shared_dir),
            run_timestamp=_parse_iso(request_spec.get("timestamp")),
            raw=request_spec,
        )
    return runs


def _load_traces() -> list[TraceRecord]:
    records: list[TraceRecord] = []
    if not TRACE_ROOT.exists():
        return records

    for path in TRACE_ROOT.rglob("llm_traces.jsonl"):
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    raw = json.loads(line)
                    run_id = str(raw.get("run_id") or "").strip()
                    timestamp = _parse_iso(raw.get("timestamp"))
                    if not timestamp:
                        continue
                    search_text = " ".join(
                        str(raw.get(key) or "") for key in ("doc_name", "user_msg", "response")
                    ).lower()
                    records.append(
                        TraceRecord(
                            run_id=run_id,
                            doc_name=str(raw.get("doc_name") or ""),
                            agent=str(raw.get("agent") or ""),
                            deployment=str(raw.get("deployment") or ""),
                            timestamp=timestamp,
                            prompt_tokens=int(raw.get("prompt_tokens") or 0),
                            completion_tokens=int(raw.get("completion_tokens") or 0),
                            total_cost_usd=float(raw.get("total_cost_usd") or 0.0),
                            search_text=search_text,
                        )
                    )
        except FileNotFoundError:
            continue
    return sorted(records, key=lambda item: item.timestamp)


def _aggregate_models(records: list[TraceRecord]) -> list[ModelUsageResponse]:
    models: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "model_id": "",
            "model_name": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_requests": 0,
            "cost": 0.0,
        }
    )
    for record in records:
        key = record.deployment.lower()
        row = models[key]
        row["model_id"] = key
        row["model_name"] = MODEL_NAMES.get(key, record.deployment)
        row["input_tokens"] += record.prompt_tokens
        row["output_tokens"] += record.completion_tokens
        row["total_requests"] += 1
        row["cost"] += record.total_cost_usd

    return [
        ModelUsageResponse(
            modelId=row["model_id"],
            modelName=row["model_name"],
            inputTokens=row["input_tokens"],
            outputTokens=row["output_tokens"],
            totalRequests=row["total_requests"],
            cost=_round_cost(row["cost"]),
        )
        for row in sorted(models.values(), key=lambda item: item["cost"], reverse=True)
    ]


def _aggregate_stages(records: list[TraceRecord]) -> list[StageBreakdownResponse]:
    stages: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "stage_key": "other",
            "stage_name": "Other",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
            "requests": 0,
        }
    )
    for record in records:
        stage_key, stage_name = AGENT_STAGE_MAP.get(record.agent, ("other", "Other"))
        row = stages[stage_key]
        row["stage_key"] = stage_key
        row["stage_name"] = stage_name
        row["input_tokens"] += record.prompt_tokens
        row["output_tokens"] += record.completion_tokens
        row["cost"] += record.total_cost_usd
        row["requests"] += 1

    preferred_order = {
        "to_generation": 0,
        "content_generation": 1,
        "assessment_generation": 2,
        "image_generation": 3,
        "metadata_generation": 4,
        "search_operations": 5,
        "other": 6,
    }
    return [
        StageBreakdownResponse(
            stageKey=row["stage_key"],
            stageName=row["stage_name"],
            inputTokens=row["input_tokens"],
            outputTokens=row["output_tokens"],
            cost=_round_cost(row["cost"]),
            requests=row["requests"],
        )
        for row in sorted(stages.values(), key=lambda item: preferred_order[item["stage_key"]])
    ]


def _build_document(run_id: str, metadata: RunMetadata, records: list[TraceRecord]) -> DocumentCostResponse:
    total_input = sum(record.prompt_tokens for record in records)
    total_output = sum(record.completion_tokens for record in records)
    total_cost = sum(record.total_cost_usd for record in records)
    last_updated = max((record.timestamp for record in records), default=metadata.run_timestamp or datetime.now(UTC))
    models = _aggregate_models(records)
    stages = _aggregate_stages(records)

    return DocumentCostResponse(
        documentId=run_id,
        documentName=metadata.document_name,
        documentType=metadata.document_type,
        status=metadata.status,
        totalCost=_round_cost(total_cost),
        inputTokens=total_input,
        outputTokens=total_output,
        totalRequests=len(records),
        modelsUsed=[model.modelName for model in models],
        lastUpdated=last_updated.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        modelBreakdown=models,
        stageBreakdown=stages,
    )


def _build_cost_trend(records: list[TraceRecord]) -> list[CostingTrendPointResponse]:
    daily: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"cost": 0.0, "input_tokens": 0, "output_tokens": 0}
    )
    for record in records:
        key = record.timestamp.astimezone(UTC).date().isoformat()
        row = daily[key]
        row["cost"] += record.total_cost_usd
        row["input_tokens"] += record.prompt_tokens
        row["output_tokens"] += record.completion_tokens

    return [
        CostingTrendPointResponse(
            date=day,
            cost=_round_cost(values["cost"]),
            inputTokens=values["input_tokens"],
            outputTokens=values["output_tokens"],
        )
        for day, values in sorted(daily.items())
    ]


def _percentage_change(current: float, previous: float) -> float:
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return round(((current - previous) / previous) * 100, 2)


def _calculate_period_changes(documents: list[DocumentCostResponse]) -> tuple[float, float]:
    now = datetime.now(UTC)
    current_start = now - timedelta(days=30)
    previous_start = now - timedelta(days=60)

    current_docs = [
        doc for doc in documents
        if _parse_iso(doc.lastUpdated) and _parse_iso(doc.lastUpdated) >= current_start
    ]
    previous_docs = [
        doc for doc in documents
        if _parse_iso(doc.lastUpdated) and previous_start <= _parse_iso(doc.lastUpdated) < current_start
    ]

    current_cost = sum(doc.totalCost for doc in current_docs)
    previous_cost = sum(doc.totalCost for doc in previous_docs)
    return (
        _percentage_change(current_cost, previous_cost),
        _percentage_change(float(len(current_docs)), float(len(previous_docs))),
    )


class CostingService:
    def __init__(self) -> None:
        self._run_metadata = _load_run_metadata()
        self._traces = _load_traces()

    def _trace_backed_records(self) -> list[TraceRecord]:
        return [record for record in self._traces if record.run_id]

    def _records_by_run(self) -> dict[str, list[TraceRecord]]:
        grouped: dict[str, list[TraceRecord]] = defaultdict(list)
        for record in self._trace_backed_records():
            if record.run_id:
                grouped[record.run_id].append(record)
        return grouped

    def list_documents(self) -> list[DocumentCostResponse]:
        grouped = self._records_by_run()
        documents: list[DocumentCostResponse] = []
        for run_id, records in grouped.items():
            metadata = self._run_metadata.get(run_id)
            if not metadata:
                metadata = RunMetadata(
                    document_id=run_id,
                    document_name=records[0].doc_name or run_id,
                    document_type="Unknown",
                    status="completed",
                    run_timestamp=min((record.timestamp for record in records), default=None),
                    raw={},
                )
            documents.append(_build_document(run_id, metadata, records))
        documents.sort(key=lambda item: item.lastUpdated, reverse=True)
        return documents

    def get_document(self, document_id: str) -> DocumentCostResponse | None:
        grouped = self._records_by_run()
        records = grouped.get(document_id)
        if not records:
            return None
        metadata = self._run_metadata.get(document_id) or RunMetadata(
            document_id=document_id,
            document_name=records[0].doc_name or document_id,
            document_type="Unknown",
            status="completed",
            run_timestamp=min((record.timestamp for record in records), default=None),
            raw={},
        )
        return _build_document(document_id, metadata, records)

    def get_model_summary(self) -> list[ModelUsageResponse]:
        return _aggregate_models(self._trace_backed_records())

    def get_cost_trends(self) -> list[CostingTrendPointResponse]:
        return _build_cost_trend(self._trace_backed_records())

    def get_summary(self) -> CostingSummaryResponse:
        documents = self.list_documents()
        total_cost = sum(doc.totalCost for doc in documents)
        total_input = sum(doc.inputTokens for doc in documents)
        total_output = sum(doc.outputTokens for doc in documents)
        days_elapsed = max(datetime.now(UTC).day, 1)
        estimated_monthly_cost = (total_cost / days_elapsed) * 30
        cost_change_percent, docs_change_percent = _calculate_period_changes(documents)
        return CostingSummaryResponse(
            totalCost=_round_cost(total_cost),
            totalInputTokens=total_input,
            totalOutputTokens=total_output,
            totalDocumentsProcessed=len(documents),
            averageCostPerDocument=_round_cost(total_cost / len(documents)) if documents else 0.0,
            estimatedMonthlyCost=_round_cost(estimated_monthly_cost),
            costTrend=self.get_cost_trends(),
            modelSummary=self.get_model_summary(),
            documents=documents,
            costChangePercent=cost_change_percent,
            documentsChangePercent=docs_change_percent,
        )
