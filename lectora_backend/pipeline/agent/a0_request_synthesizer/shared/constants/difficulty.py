"""Shared difficulty-level constants for A0 pipeline steps."""

DEFAULT_TO_DURATION_HOURS: int = 3

_DIFFICULTY_MULTIPLIERS: dict[str, float] = {
    "basic":        1.00,
    "intermediate": 1.25,
    "advanced":     1.50,
}
