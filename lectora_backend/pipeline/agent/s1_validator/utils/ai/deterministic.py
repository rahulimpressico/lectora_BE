from __future__ import annotations

import re
from typing import Any, Literal

from .models import MissingTopic, ValidationIssue

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "in",
        "on",
        "to",
        "for",
        "with",
        "that",
        "this",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "as",
        "at",
        "by",
        "from",
        "its",
        "it",
        "their",
        "they",
        "will",
        "shall",
        "about",
        "into",
        "through",
        "during",
    }
)
_COVERAGE_THRESHOLD = 0.60
_PARTIAL_THRESHOLD = 0.25


class RequiredTopicCoverage:
    __slots__ = ("topic", "status", "matched_fraction", "found_in_sections")

    def __init__(
        self,
        topic: str,
        status: str,
        matched_fraction: float,
        found_in_sections: list[str],
    ) -> None:
        self.topic = topic
        self.status = status
        self.matched_fraction = matched_fraction
        self.found_in_sections = found_in_sections


def _topic_keywords(topic: str) -> list[str]:
    raw = re.findall(r"[a-zA-Z0-9]+", topic.lower())
    return [w for w in raw if w not in _STOP_WORDS and len(w) > 1]


def _outline_text_tokens(sections: list[dict[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    for sec in sections:
        tokens.update(_topic_keywords(sec.get("title") or ""))
        for sub in sec.get("subtopics") or []:
            tokens.update(_topic_keywords(str(sub)))
    return tokens


def _check_required_topics_deterministic(
    required_topics: list[str],
    sections: list[dict[str, Any]],
) -> list[RequiredTopicCoverage]:
    if not required_topics or not sections:
        return []

    outline_tokens = _outline_text_tokens(sections)
    results: list[RequiredTopicCoverage] = []

    for topic in required_topics:
        keywords = _topic_keywords(topic)
        if not keywords:
            continue

        matched = [kw for kw in keywords if kw in outline_tokens]
        fraction = len(matched) / len(keywords)

        if fraction >= _COVERAGE_THRESHOLD:
            status = "covered"
        elif fraction >= _PARTIAL_THRESHOLD:
            status = "partial"
        else:
            status = "missing"

        found_in: list[str] = []
        for sec in sections:
            sec_tokens = _topic_keywords(sec.get("title") or "")
            if any(kw in sec_tokens for kw in matched):
                found_in.append(sec.get("title") or f"sec_{sec.get('index', '?')}")

        results.append(
            RequiredTopicCoverage(
                topic=topic,
                status=status,
                matched_fraction=fraction,
                found_in_sections=found_in,
            )
        )

    return results


def _required_topics_to_issues(
    coverages: list[RequiredTopicCoverage],
) -> tuple[list[ValidationIssue], list[MissingTopic]]:
    issues: list[ValidationIssue] = []
    missing: list[MissingTopic] = []

    uncovered = [c for c in coverages if c.status != "covered"]
    if not uncovered:
        return issues, missing

    for cov in uncovered:
        pct = int(cov.matched_fraction * 100)
        if cov.status == "missing":
            severity: Literal["blocker", "warning", "info"] = "blocker"
            msg = (
                f"Required topic '{cov.topic}' is completely absent from the outline "
                f"(0 % keyword match). This topic was explicitly requested by the user."
            )
            missing.append(
                MissingTopic(
                    topic=cov.topic,
                    reason="Completely absent — no keywords from this required topic appear in any section.",
                    severity="high",
                )
            )
        else:
            severity = "warning"
            sections_str = ", ".join(cov.found_in_sections[:3]) or "none identified"
            msg = (
                f"Required topic '{cov.topic}' is only partially covered in the outline "
                f"({pct} % keyword match). Partially matched in: {sections_str}."
            )

        issues.append(
            ValidationIssue(
                field=f"required_topics.coverage.{cov.topic[:40].replace(' ', '_')}",
                expected=f"Required topic '{cov.topic}' fully present in outline",
                found=f"{pct}% keyword match ({cov.status})",
                severity=severity,
                message=msg,
                rule_source="user_requirements.required_topics",
                failure_reason=(
                    f"User specified '{cov.topic}' as a mandatory topic via the course wizard, "
                    f"but it is {'absent' if cov.status == 'missing' else 'insufficiently represented'} "
                    f"in the generated outline."
                ),
                remediation=(
                    f"Add a dedicated section or sub-section covering '{cov.topic}'. "
                    "Ensure all user-required topics have explicit coverage."
                ),
            )
        )

    return issues, missing


__all__ = [
    "_check_required_topics_deterministic",
    "_required_topics_to_issues",
]
