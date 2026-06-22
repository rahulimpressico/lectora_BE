"""Step 08 — Persist course_spec to shared state and disk."""
import json
import logging
import os as _os
from datetime import datetime, timezone
from pathlib import Path

from ...shared.models.state import A1State

logger = logging.getLogger(__name__)


def _write_terminal(state: A1State, label: str) -> None:
    output_dir = Path(state["shared_state_path"]).expanduser().resolve().parent
    path = output_dir / f"a1_{label}.json"
    with open(path, "w") as f:
        json.dump(
            {
                "status": label.upper(),
                "reason": state.get("error"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            f, indent=2,
        )


def persist_output(state: A1State) -> A1State:
    if state["status"] in ("failed", "stopped"):
        return state

    logger.info("[A1] Persisting to shared state...")
    a1_output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "course_spec": state["course_spec"],
        "inconsistencies": state.get("inconsistencies", []),
    }

    with open(state["shared_state_path"]) as f:
        shared = json.load(f)
    shared["agent_outputs"]["A1"] = a1_output
    shared["status"] = "A1_complete"
    _tmp_path = state["shared_state_path"] + ".tmp"
    with open(_tmp_path, "w") as f:
        json.dump(shared, f, indent=2, default=str)
    _os.replace(_tmp_path, state["shared_state_path"])

    output_dir = Path(state["shared_state_path"]).expanduser().resolve().parent
    spec_path = output_dir / "course_spec.json"
    with open(spec_path, "w") as f:
        json.dump(a1_output, f, indent=2, default=str)

    logger.info("[A1] course_spec written -> %s", spec_path)
    return {**state, "status": "complete"}


def failed_end(state: A1State) -> A1State:
    logger.error("[A1] FAILED: %s", state.get("error"))
    _write_terminal(state, "failed")
    return {**state, "status": "failed"}


def stopped_end(state: A1State) -> A1State:
    logger.warning("[A1] STOPPED: %s", state.get("error"))
    _write_terminal(state, "stopped")
    return {**state, "status": "stopped"}
