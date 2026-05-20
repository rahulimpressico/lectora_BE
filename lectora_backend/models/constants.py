"""Pipeline-wide constants shared across API, worker, and pipeline modules."""

from lectora_backend.models.job_enums import PipelineStep

# Ordered list of pipeline stages (used for DB stage scaffolding + API ordering).
# Single source of truth — import from here instead of redefining in each module.
PIPELINE_ORDER: list[PipelineStep] = [
    PipelineStep.A0,
    PipelineStep.A1,
    PipelineStep.S1,
    PipelineStep.A2,
    PipelineStep.A3,
    PipelineStep.A4,
    PipelineStep.A5,
    PipelineStep.S2,
    PipelineStep.A6,
]

# Gate cycle limits — how many times each gate loop can retry before hard-stopping.
MAX_S1_GATE_CYCLES: int = 3   # A0 → A1 → S1 cycles
MAX_A2_S2_CYCLES: int = 3     # A2 → S2 cycles
MAX_A0_A1_S1_CYCLES: int = 3  # alias used by local pipeline runner (pipeline.py)
