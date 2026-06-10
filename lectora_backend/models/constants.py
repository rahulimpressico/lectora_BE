"""Pipeline-wide constants shared across API, worker, and pipeline modules."""

from lectora_backend.models.job_enums import PipelineStep

# Ordered list of pipeline stages (used for DB stage scaffolding + API ordering).
# Single source of truth — import from here instead of redefining in each module.
PIPELINE_ORDER: list[PipelineStep] = [
    PipelineStep.A0,
    PipelineStep.A1,
    PipelineStep.S1,
    PipelineStep.SECTION_MAPPER,
    PipelineStep.KC_PLANNER,
    PipelineStep.A2,
    PipelineStep.S2,
]

# Precomputed O(1) stage-position lookup — use instead of PIPELINE_ORDER.index()
# which raises ValueError for unknown stages and is O(n) per call.
STAGE_ORDER: dict[PipelineStep, int] = {
    step: i for i, step in enumerate(PIPELINE_ORDER)
}

# Gate cycle limits — how many times each gate loop can retry before hard-stopping.
MAX_S1_GATE_CYCLES: int = 3   # A0 → A1 → S1 cycles
MAX_A2_S2_CYCLES: int = 3     # A2 → S2 cycles
MAX_A0_A1_S1_CYCLES: int = 3  # alias used by local pipeline runner (pipeline.py)
