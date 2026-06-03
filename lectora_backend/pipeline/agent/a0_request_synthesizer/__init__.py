"""A0 — Request Synthesizer package."""

__all__ = ["A0RequestSynthesizer"]


def __getattr__(name: str):
    if name == "A0RequestSynthesizer":
        from .main import A0RequestSynthesizer

        return A0RequestSynthesizer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
