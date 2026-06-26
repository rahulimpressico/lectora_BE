from __future__ import annotations

"""
LLM prompt and response tracer
──────────────────────────────
Persists a full record of each LLM call as JSON Lines (JSONL).

Trace files are written under:

  logs/{doc_name}/{agent}/llm_traces.jsonl

Where:
  - doc_name: source document stem the pipeline ran for
  - agent:    which agent invoked the call (A0 / A1 / A2 / S1 / S2)

Each file is JSONL (one line = one JSON object per LLM call).

Each record includes:
  - timestamp         : UTC time of the call
  - run_id            : unique id for the pipeline run
  - doc_name          : source document stem the pipeline ran for
  - agent             : which agent invoked the call (A0 / A1 / A2 / S1 / S2)
  - deployment        : Azure model / deployment name
  - latency_ms        : wall-clock duration of the API call
  - prompt_tokens     : tokens for system + user messages
  - cached_prompt_tokens : cached prompt tokens (if available; else 0)
  - completion_tokens : tokens in the model reply
  - total_tokens      : sum of prompt and completion tokens
  - input_cost_usd    : computed USD cost for prompt tokens
  - output_cost_usd   : computed USD cost for completion tokens
  - total_cost_usd    : input_cost_usd + output_cost_usd
  - system_prompt     : full system message sent to the model
  - user_msg          : full user message sent to the model
  - response          : full model response text
  - error             : error message if the call failed, else null
"""

import json
import logging
import re
import threading
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from lectora_backend.config import settings

try:
    from langfuse import Langfuse
except Exception:  # pragma: no cover - import guarded for environments without langfuse
    Langfuse = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Context variables — set from pipeline.py; read here on each trace
# ---------------------------------------------------------------------------

_ctx_run_id: ContextVar[str] = ContextVar("run_id", default="")
_ctx_doc_name: ContextVar[str] = ContextVar("doc_name", default="")
_ctx_source_refs: ContextVar[tuple[str, ...]] = ContextVar("source_refs", default=())


def set_run_context(run_id: str, doc_name: str, source_refs: list[str] | tuple[str, ...] | None = None) -> None:
    """
    Call at pipeline start so all LLM calls in this thread / async task
    inherit run_id and doc_name on their trace records.
    """
    _ctx_run_id.set(run_id)
    _ctx_doc_name.set(doc_name)
    if source_refs is not None:
        set_source_refs(source_refs)


def set_run_id(run_id: str) -> None:
    """Update only run_id in the current context."""
    _ctx_run_id.set(run_id)


def set_doc_name(doc_name: str) -> None:
    """Update only doc_name in the current context."""
    _ctx_doc_name.set(doc_name)


def set_source_refs(source_refs: list[str] | tuple[str, ...] | str | None) -> None:
    """Update source document / blob references in the current trace context."""
    if source_refs is None:
        _ctx_source_refs.set(())
        return
    if isinstance(source_refs, str):
        refs = (source_refs,)
    else:
        refs = tuple(str(ref).strip() for ref in source_refs if str(ref).strip())
    _ctx_source_refs.set(refs)


def get_run_id() -> str:
    return _ctx_run_id.get()


def get_doc_name() -> str:
    return _ctx_doc_name.get()


def get_source_refs() -> list[str]:
    return list(_ctx_source_refs.get())


def submit_with_trace_context(executor, fn, /, *args, **kwargs):
    """Run work in a thread-pool worker while preserving the current trace context."""
    ctx = copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# LLMTrace dataclass
# ---------------------------------------------------------------------------


@dataclass
class LLMTrace:
    agent: str
    deployment: str
    system_prompt: str
    user_msg: str
    response: str
    latency_ms: float
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    error: Optional[str] = None
    source_refs: list[str] = field(default_factory=get_source_refs)
    model_parameters: dict[str, Any] = field(default_factory=dict)
    prompt_metadata: dict[str, Any] = field(default_factory=dict)
    observation_name: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    run_id: str = field(default_factory=get_run_id)
    doc_name: str = field(default_factory=get_doc_name)


# ---------------------------------------------------------------------------
# EmbeddingTrace dataclass
# ---------------------------------------------------------------------------


@dataclass
class EmbeddingTrace:
    """Trace record for a single embedding API batch call."""

    agent: str                          # e.g. "EMBED", "INGEST_EMBED"
    deployment: str                     # embedding model / deployment name
    level: str                          # "title" | "summary" | "content" | "keywords"
    batch_index: int                    # 0-based index of this batch within the level
    batch_size: int                     # number of texts in the batch
    dimensions: int                     # embedding vector dimensions
    latency_ms: float
    total_tokens: int = 0               # from response.usage.total_tokens when available
    error: Optional[str] = None
    document_id: Optional[str] = None   # ingestion document being embedded
    source_refs: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    run_id: str = field(default_factory=get_run_id)
    doc_name: str = field(default_factory=get_doc_name)


# ---------------------------------------------------------------------------
# RetrievalTrace dataclass
# ---------------------------------------------------------------------------


@dataclass
class RetrievalTrace:
    """Trace record for a single vector / hybrid search call."""

    agent: str                          # e.g. "SECTION_MAPPER", "VECTOR_RETRIEVAL"
    retrieval_type: str                 # "lesson_hybrid" | "subtopic_semantic" | "keyword_fallback"
    query: str
    result_count: int
    latency_ms: float
    top_score: Optional[float] = None
    threshold: Optional[float] = None
    has_semantic_ranker: bool = False
    document_id: Optional[str] = None
    error: Optional[str] = None
    source_refs: List[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    run_id: str = field(default_factory=get_run_id)
    doc_name: str = field(default_factory=get_doc_name)


# ---------------------------------------------------------------------------
# Billing config (USD per 1M tokens)
# ---------------------------------------------------------------------------

_PRICES_PER_1M = {
    # keys are normalized to lowercase
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "o3": {"input": 2.00, "cached_input": 0.50, "output": 8.00},
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
}

# ---------------------------------------------------------------------------
# Log path helpers
# ---------------------------------------------------------------------------

_LOGS_ROOT = Path(__file__).resolve().parent.parent / "logs"
_write_lock = threading.Lock()
_langfuse_lock = threading.Lock()
_langfuse_client: Langfuse | None | bool = None
_langfuse_warning_emitted = False
logger = logging.getLogger(__name__)


def _ensure_log_dir() -> None:
    _LOGS_ROOT.mkdir(parents=True, exist_ok=True)


def _safe_dir_name(name: str) -> str:
    """Filesystem-safe directory name (keeps dots/dashes/underscores)."""
    s = (name or "").strip()
    s = re.sub(r"[^\w.\-]+", "_", s, flags=re.UNICODE).strip("._")
    return s or "unknown"


def _trace_file_path(doc_name: str, agent: str) -> Path:
    safe_doc = _safe_dir_name(doc_name)
    safe_agent = _safe_dir_name(agent or "unknown_agent")
    return _LOGS_ROOT / safe_doc / safe_agent / "llm_traces.jsonl"


def _compute_cost_usd(
    deployment: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_prompt_tokens: int = 0,
) -> tuple[float, float, float]:
    key = (deployment or "").strip().lower()
    if key == "gpt-5.4":
        key = "gpt-5.4-mini"
    prices = _PRICES_PER_1M.get(key)
    if not prices:
        return 0.0, 0.0, 0.0

    billed_prompt = max(int(prompt_tokens) - int(cached_prompt_tokens), 0)
    input_cost = (billed_prompt / 1_000_000) * prices["input"] + (
        int(cached_prompt_tokens) / 1_000_000) * prices["cached_input"]
    output_cost = (int(completion_tokens) / 1_000_000) * prices["output"]
    total_cost = input_cost + output_cost
    return input_cost, output_cost, total_cost


def _parse_langfuse_api_key(api_key: str) -> tuple[str, str] | tuple[None, None]:
    raw = (api_key or "").strip()
    if not raw:
        return None, None
    for sep in (":", "|", ","):
        if sep in raw:
            public_key, secret_key = raw.split(sep, 1)
            public_key = public_key.strip()
            secret_key = secret_key.strip()
            if public_key and secret_key:
                return public_key, secret_key
    return None, None


def _get_langfuse_credentials() -> tuple[str, str, str, str, str]:
    public_key = (settings.langfuse_public_key or "").strip()
    secret_key = (settings.langfuse_secret_key or "").strip()
    host = (
        (settings.langfuse_host or "").strip()
        or (settings.langfuse_base_url or "").strip()
        or "https://cloud.langfuse.com"
    )
    project = (settings.langfuse_project or "").strip()
    environment = (settings.langfuse_env or "").strip()

    alias_public, alias_secret = _parse_langfuse_api_key(settings.langfuse_api_key)
    if alias_public and alias_secret and not (public_key and secret_key):
        public_key = alias_public
        secret_key = alias_secret

    return public_key, secret_key, host, project, environment


def _get_langfuse_client() -> Langfuse | None:
    global _langfuse_client, _langfuse_warning_emitted

    if _langfuse_client is False:
        return None
    if _langfuse_client is not None:
        return _langfuse_client
    if Langfuse is None:
        _langfuse_client = False
        return None

    public_key, secret_key, host, _, environment = _get_langfuse_credentials()
    if not public_key or not secret_key:
        _langfuse_client = False
        return None

    with _langfuse_lock:
        if _langfuse_client is not None and _langfuse_client is not False:
            return _langfuse_client
        if _langfuse_client is False:
            return None
        try:
            _langfuse_client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host,
                environment=environment or None,
            )
            return _langfuse_client
        except Exception as exc:  # pragma: no cover - defensive path
            _langfuse_client = False
            if not _langfuse_warning_emitted:
                _langfuse_warning_emitted = True
                logger.warning("[langfuse] init failed: %s", exc)
            return None


def flush_langfuse() -> None:
    client = _get_langfuse_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        pass


def shutdown_langfuse() -> None:
    client = _get_langfuse_client()
    if client is None:
        return
    try:
        client.shutdown()
    except Exception:
        pass


def _write_langfuse_trace(trace: LLMTrace) -> None:
    client = _get_langfuse_client()
    if client is None:
        return

    trace_seed = trace.run_id or f"{trace.doc_name}:{trace.agent}"
    trace_id = client.create_trace_id(seed=trace_seed)

    metadata = {
        "timestamp": trace.timestamp,
        "run_id": trace.run_id,
        "doc_name": trace.doc_name,
        "agent": trace.agent,
        "source_refs": trace.source_refs,
        "prompt_metadata": trace.prompt_metadata,
        "langfuse_project": (settings.langfuse_project or "").strip() or None,
        "error": trace.error,
    }
    metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {}, ())}

    prompt_block = {
        "system_prompt_exact": trace.system_prompt,
        "user_prompt_exact": trace.user_msg,
    }
    prompt_block = {k: v for k, v in prompt_block.items() if v not in (None, "")}

    callsite_block = {
        "file": (trace.prompt_metadata or {}).get("prompt_file"),
        "function": (trace.prompt_metadata or {}).get("prompt_function"),
        "line": (trace.prompt_metadata or {}).get("prompt_line"),
    }
    callsite_block = {k: v for k, v in callsite_block.items() if v not in (None, "")}

    prompt_input = {
        "agent": trace.agent,
        "prompt": prompt_block,
        "input_document_or_blob": trace.source_refs,
        "prompt_source": callsite_block or None,
        "prompt_metadata": trace.prompt_metadata or None,
        "model_parameters": {
            key: value
            for key, value in (trace.model_parameters or {}).items()
            if value is not None
        } or None,
    }
    prompt_input = {k: v for k, v in prompt_input.items() if v not in (None, "", [], {}, ())}

    usage_details = {
        "input": int(trace.prompt_tokens),
        "output": int(trace.completion_tokens),
        "total": int(trace.total_tokens),
    }
    cost_details = {
        "input_cost_usd": float(trace.input_cost_usd),
        "output_cost_usd": float(trace.output_cost_usd),
        "total_cost_usd": float(trace.total_cost_usd),
    }
    model_parameters = {
        key: value
        for key, value in (trace.model_parameters or {}).items()
        if value is not None
    }
    output_payload = {
        "agent": trace.agent,
        "output_exact": trace.response,
    }
    if trace.error:
        output_payload["error"] = trace.error

    with client.start_as_current_observation(
        trace_context={"trace_id": trace_id},
        name=trace.observation_name or f"{trace.agent} | prompt -> output",
        as_type="generation",
        input=prompt_input,
        output=output_payload,
        metadata=metadata,
        level="ERROR" if trace.error else "DEFAULT",
        status_message=trace.error,
        completion_start_time=datetime.fromisoformat(trace.timestamp),
        model=trace.deployment,
        model_parameters=model_parameters or None,
        usage_details=usage_details,
        cost_details=cost_details,
    ):
        pass


# ---------------------------------------------------------------------------
# Langfuse writers for embedding + retrieval
# ---------------------------------------------------------------------------


def _write_langfuse_embedding_observation(trace: EmbeddingTrace) -> None:
    """Send an embedding batch call as a Langfuse generation observation."""
    client = _get_langfuse_client()
    if client is None:
        return

    trace_seed = trace.run_id or f"{trace.doc_name}:EMBED"
    trace_id = client.create_trace_id(seed=trace_seed)

    usage_details = {"total": int(trace.total_tokens)}
    metadata = {
        "timestamp": trace.timestamp,
        "run_id": trace.run_id,
        "doc_name": trace.doc_name,
        "agent": trace.agent,
        "level": trace.level,
        "batch_index": trace.batch_index,
        "batch_size": trace.batch_size,
        "dimensions": trace.dimensions,
        "document_id": trace.document_id,
        "source_refs": trace.source_refs,
        "error": trace.error,
    }
    metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {}, ())}

    prompt_input = {
        "agent": trace.agent,
        "level": trace.level,
        "batch_index": trace.batch_index,
        "batch_size": trace.batch_size,
    }
    output_payload: dict = {
        "agent": trace.agent,
        "dimensions": trace.dimensions,
        "vectors_produced": trace.batch_size if not trace.error else 0,
    }
    if trace.error:
        output_payload["error"] = trace.error

    with client.start_as_current_observation(
        trace_context={"trace_id": trace_id},
        name=f"{trace.agent} | embed {trace.level} batch {trace.batch_index}",
        as_type="generation",
        input=prompt_input,
        output=output_payload,
        metadata=metadata,
        level="ERROR" if trace.error else "DEFAULT",
        status_message=trace.error,
        completion_start_time=datetime.fromisoformat(trace.timestamp),
        model=trace.deployment,
        usage_details=usage_details,
    ):
        pass


def _write_langfuse_retrieval_span(trace: RetrievalTrace) -> None:
    """Send a vector / hybrid search call as a Langfuse span observation."""
    client = _get_langfuse_client()
    if client is None:
        return

    trace_seed = trace.run_id or f"{trace.doc_name}:{trace.agent}"
    trace_id = client.create_trace_id(seed=trace_seed)

    metadata = {
        "timestamp": trace.timestamp,
        "run_id": trace.run_id,
        "doc_name": trace.doc_name,
        "agent": trace.agent,
        "retrieval_type": trace.retrieval_type,
        "result_count": trace.result_count,
        "top_score": trace.top_score,
        "threshold": trace.threshold,
        "has_semantic_ranker": trace.has_semantic_ranker,
        "document_id": trace.document_id,
        "source_refs": trace.source_refs,
        "extra": trace.metadata,
        "error": trace.error,
    }
    metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {}, ())}

    span_input = {
        "agent": trace.agent,
        "retrieval_type": trace.retrieval_type,
        "query": trace.query[:500] if trace.query else "",
        "document_id": trace.document_id,
        "has_semantic_ranker": trace.has_semantic_ranker,
    }
    span_input = {k: v for k, v in span_input.items() if v not in (None, "", {}, ())}

    output_payload: dict = {
        "result_count": trace.result_count,
        "top_score": trace.top_score,
        "threshold": trace.threshold,
    }
    if trace.error:
        output_payload["error"] = trace.error
    output_payload = {k: v for k, v in output_payload.items() if v is not None}

    with client.start_as_current_observation(
        trace_context={"trace_id": trace_id},
        name=f"{trace.agent} | {trace.retrieval_type}",
        as_type="span",
        input=span_input,
        output=output_payload,
        metadata=metadata,
        level="ERROR" if trace.error else "DEFAULT",
        status_message=trace.error,
        completion_start_time=datetime.fromisoformat(trace.timestamp),
    ):
        pass


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def write_embedding_trace(trace: EmbeddingTrace) -> None:
    """Append one embedding trace record to JSONL and send to Langfuse."""
    _ensure_log_dir()
    safe_doc = _safe_dir_name(trace.doc_name or "ingestion")
    log_path = _LOGS_ROOT / safe_doc / (trace.agent or "EMBED") / "embedding_traces.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "timestamp": trace.timestamp,
        "run_id": trace.run_id,
        "doc_name": trace.doc_name,
        "agent": trace.agent,
        "deployment": trace.deployment,
        "level": trace.level,
        "batch_index": trace.batch_index,
        "batch_size": trace.batch_size,
        "dimensions": trace.dimensions,
        "latency_ms": round(trace.latency_ms, 2),
        "total_tokens": trace.total_tokens,
        "document_id": trace.document_id,
        "source_refs": trace.source_refs,
        "error": trace.error,
    }
    with _write_lock:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        _write_langfuse_embedding_observation(trace)
    except Exception as exc:
        logger.warning("[langfuse] failed to write embedding trace: %s", exc)


def write_retrieval_trace(trace: RetrievalTrace) -> None:
    """Append one retrieval trace record to JSONL and send to Langfuse."""
    _ensure_log_dir()
    safe_doc = _safe_dir_name(trace.doc_name or "pipeline")
    log_path = _LOGS_ROOT / safe_doc / (trace.agent or "RETRIEVAL") / "retrieval_traces.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "timestamp": trace.timestamp,
        "run_id": trace.run_id,
        "doc_name": trace.doc_name,
        "agent": trace.agent,
        "retrieval_type": trace.retrieval_type,
        "query": trace.query,
        "result_count": trace.result_count,
        "latency_ms": round(trace.latency_ms, 2),
        "top_score": trace.top_score,
        "threshold": trace.threshold,
        "has_semantic_ranker": trace.has_semantic_ranker,
        "document_id": trace.document_id,
        "source_refs": trace.source_refs,
        "metadata": trace.metadata,
        "error": trace.error,
    }
    with _write_lock:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        _write_langfuse_retrieval_span(trace)
    except Exception as exc:
        logger.warning("[langfuse] failed to write retrieval trace: %s", exc)


def write_trace(trace: LLMTrace) -> None:
    """Append one trace record to the JSONL log file (thread-safe)."""
    _ensure_log_dir()
    trace_path = _trace_file_path(trace.doc_name, trace.agent)
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    input_cost, output_cost, total_cost = _compute_cost_usd(
        deployment=trace.deployment,
        prompt_tokens=trace.prompt_tokens,
        completion_tokens=trace.completion_tokens,
        cached_prompt_tokens=trace.cached_prompt_tokens,
    )
    trace.input_cost_usd = input_cost
    trace.output_cost_usd = output_cost
    trace.total_cost_usd = total_cost

    record = {
        "timestamp": trace.timestamp,
        "run_id": trace.run_id,
        "doc_name": trace.doc_name,
        "agent": trace.agent,
        "deployment": trace.deployment,
        "latency_ms": round(trace.latency_ms, 2),
        "prompt_tokens": trace.prompt_tokens,
        "cached_prompt_tokens": trace.cached_prompt_tokens,
        "completion_tokens": trace.completion_tokens,
        "total_tokens": trace.total_tokens,
        "input_cost_usd": round(trace.input_cost_usd, 8),
        "output_cost_usd": round(trace.output_cost_usd, 8),
        "total_cost_usd": round(trace.total_cost_usd, 8),
        "error": trace.error,
        "source_refs": trace.source_refs,
        "model_parameters": trace.model_parameters,
        "prompt_metadata": trace.prompt_metadata,
        "observation_name": trace.observation_name,
        "system_prompt": trace.system_prompt,
        "user_msg": trace.user_msg,
        "response": trace.response,
    }
    with _write_lock:
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        _write_langfuse_trace(trace)
    except Exception as exc:
        logger.warning("[langfuse] failed to write trace: %s", exc)
