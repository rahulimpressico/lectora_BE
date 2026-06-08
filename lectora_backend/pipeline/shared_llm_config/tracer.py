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
import re
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Context variables — set from pipeline.py; read here on each trace
# ---------------------------------------------------------------------------

_ctx_run_id: ContextVar[str] = ContextVar("run_id", default="")
_ctx_doc_name: ContextVar[str] = ContextVar("doc_name", default="")


def set_run_context(run_id: str, doc_name: str) -> None:
    """
    Call at pipeline start so all LLM calls in this thread / async task
    inherit run_id and doc_name on their trace records.
    """
    _ctx_run_id.set(run_id)
    _ctx_doc_name.set(doc_name)


def set_run_id(run_id: str) -> None:
    """Update only run_id in the current context."""
    _ctx_run_id.set(run_id)


def set_doc_name(doc_name: str) -> None:
    """Update only doc_name in the current context."""
    _ctx_doc_name.set(doc_name)


def get_run_id() -> str:
    return _ctx_run_id.get()


def get_doc_name() -> str:
    return _ctx_doc_name.get()


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
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
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


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


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
        "system_prompt": trace.system_prompt,
        "user_msg": trace.user_msg,
        "response": trace.response,
    }
    with _write_lock:
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
