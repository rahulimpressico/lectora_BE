"""
S2 — Content, compliance, structure, and LO validation checks.

Validates:
  - A2 pipeline completeness (output present, no failed sections)
  - Section non-emptiness
  - Compliance: forbidden phrases, required behaviors, voice/tone
  - Content structure: intro, summary, LO listing, callouts, examples, duplicates
  - Learning-objective coverage in generated sections
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_SECOND_PERSON_PATTERN = re.compile(
    r"\b(you|your|yourself|yours|you're|you've|you'd|you'll)\b",
    re.IGNORECASE,
)

_THIRD_PERSON_PRONOUN_PATTERN = re.compile(
    r"\b("
    r"he|she|they|it|him|her|them|their|theirs|its|"
    r"himself|herself|themself|themselves"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten_text(sec: dict) -> str:
    """Flatten all text-bearing fields of a section into a single lowercase string."""
    parts: list[str] = [str(sec.get("heading") or "")]
    for block in sec.get("body_paragraphs", []) or []:
        c = block.get("content")
        if isinstance(c, str):
            parts.append(c)
        for item in block.get("items", []) or []:
            if isinstance(item, str):
                parts.append(item)
        q = block.get("question")
        if isinstance(q, str):
            parts.append(q)
        for opt in block.get("options", []) or []:
            if isinstance(opt, str):
                parts.append(opt)
        ex = block.get("explanation")
        if isinstance(ex, str):
            parts.append(ex)
    return "\n".join(parts).lower()


def _flatten_course_lower(sections: list[dict]) -> str:
    """All section text, lowercased (whole course)."""
    return "\n".join(_flatten_text(s) for s in sections)


def _flatten_course_raw(sections: list[dict]) -> str:
    """Original-case concatenation for markdown bold / URL checks."""
    parts: list[str] = []
    for sec in sections:
        parts.append(str(sec.get("heading") or ""))
        for block in sec.get("body_paragraphs", []) or []:
            c = block.get("content")
            if isinstance(c, str):
                parts.append(c)
            for item in block.get("items", []) or []:
                if isinstance(item, str):
                    parts.append(item)
            q = block.get("question")
            if isinstance(q, str):
                parts.append(q)
            for opt in block.get("options", []) or []:
                if isinstance(opt, str):
                    parts.append(opt)
            ex = block.get("explanation")
            if isinstance(ex, str):
                parts.append(ex)
    return "\n".join(parts)


def _parenthetical_tokens(behavior: str) -> list[str]:
    """Extract comma-separated tokens inside the last (...), e.g. SEC, FINRA, FinCEN."""
    m = re.search(r"\(([^)]+)\)", behavior)
    if not m:
        return []
    raw = m.group(1)
    out: list[str] = []
    for chunk in re.split(r"[,;]", raw):
        t = chunk.strip()
        if t:
            out.append(t)
    return out


def _l1_lessons(sections: list[dict]) -> list[dict]:
    return [s for s in sections if s.get("level") == 1 and not s.get("is_knowledge_check")]


def _section_block_counts(sec: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for block in sec.get("body_paragraphs", []) or []:
        t = block.get("type")
        if t:
            counts[t] = counts.get(t, 0) + 1
    return counts


def _range_pair(value) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# A2 pipeline completeness
# ---------------------------------------------------------------------------

def check_a2_completeness(a2_output: dict) -> list[dict]:
    """A2 must have completed and produced sections."""
    issues: list[dict] = []
    if not a2_output:
        issues.append({
            "field": "a2_output",
            "expected": "present",
            "found": "missing",
            "severity": "blocker",
            "message": "The content generation step did not produce any output. The course content is missing.",
            "rule_source": "pipeline",
        })
        return issues

    status = a2_output.get("status")
    if status not in ("complete", "partial"):
        issues.append({
            "field": "a2_output.status",
            "expected": "'complete' or 'partial'",
            "found": str(status),
            "severity": "blocker",
            "message": f"Content generation finished with an unexpected status ('{status}'). The content cannot be checked.",
            "rule_source": "pipeline",
        })

    sections = a2_output.get("sections", []) or []
    if not sections:
        issues.append({
            "field": "a2_output.sections",
            "expected": ">= 1",
            "found": 0,
            "severity": "blocker",
            "message": "Content generation produced no sections. The course appears to be empty.",
            "rule_source": "pipeline",
        })

    failed = sum(1 for s in sections if s.get("status") == "failed")
    if failed:
        issues.append({
            "field": "a2_output.sections.failed",
            "expected": "0",
            "found": failed,
            "severity": "blocker",
            "message": f"{failed} section(s) failed to generate and have no content. These must be fixed before the document can be produced.",
            "rule_source": "pipeline",
        })

    return issues


def check_section_non_empty(sections: list[dict]) -> list[dict]:
    """Every non-skipped section should have body_paragraphs."""
    issues: list[dict] = []
    for sec in sections:
        status = sec.get("status")
        if status in ("skipped", "skipped_thin"):
            continue
        body = sec.get("body_paragraphs") or []
        if not body:
            issues.append({
                "field": f"section.{sec.get('section_id') or sec.get('heading')}.body_paragraphs",
                "expected": ">= 1 block",
                "found": 0,
                "severity": "blocker",
                "message": (
                    f"Section '{sec.get('heading')}' was generated but has no content. "
                    "This section will appear blank in the final document."
                ),
                "rule_source": "A2 content_writer",
            })
    return issues


# ---------------------------------------------------------------------------
# Compliance / style checks
# ---------------------------------------------------------------------------

def check_forbidden_phrases(sections: list[dict], rule_pack: dict) -> list[dict]:
    """Scan text content for compliance_elements.forbidden_phrases."""
    issues: list[dict] = []
    compliance = rule_pack.get("compliance_elements", {}) or {}
    phrases: list[str] = [p for p in (compliance.get("forbidden_phrases") or []) if isinstance(p, str)]
    if not phrases:
        return issues

    for sec in sections:
        sid = sec.get("section_id") or sec.get("heading") or "?"
        flat = _flatten_text(sec)
        for phrase in phrases:
            needle = phrase.strip().lower()
            if needle and needle in flat:
                issues.append({
                    "field": f"section.{sid}.text",
                    "expected": f"no forbidden phrase '{phrase}'",
                    "found": phrase,
                    "severity": "blocker",
                    "message": f"Section '{sec.get('heading')}' contains a phrase that is not allowed for this course type: '{phrase}'. Please remove or rephrase it.",
                    "rule_source": "compliance_elements.forbidden_phrases",
                })
    return issues


def check_required_behaviors(sections: list[dict], rule_pack: dict) -> list[dict]:
    """
    Validate ``compliance_elements.required_behaviors`` with deterministic heuristics.

    Long natural-language rules are matched by keyword/clause dispatch. Unknown rules are skipped.
    Clause splitting on ``;`` handles combined behaviors (e.g. this course + do not use we).
    """
    issues: list[dict] = []
    raw_behaviors = (
        (rule_pack.get("compliance_elements", {}) or {}).get("required_behaviors") or []
    )
    behaviors = [b for b in raw_behaviors if isinstance(b, str) and b.strip()]
    if not behaviors:
        return issues

    course_lower = _flatten_course_lower(sections)
    course_raw = _flatten_course_raw(sections)
    cw = len(course_lower.split())
    if cw < 40:
        return issues

    seen_issue_keys: set[str] = set()

    def _add(
        field: str,
        expected: str,
        found: Any,
        severity: str,
        message: str,
        rule_src: str,
        dedupe: str,
    ) -> None:
        if dedupe in seen_issue_keys:
            return
        seen_issue_keys.add(dedupe)
        issues.append({
            "field": field,
            "expected": expected,
            "found": found,
            "severity": severity,
            "message": message,
            "rule_source": rule_src,
        })

    for behavior in behaviors:
        clauses = (
            [behavior.strip()]
            if ";" not in behavior
            else [c.strip() for c in re.split(r"\s*;\s*", behavior) if c.strip()]
        )
        for clause in clauses:
            bl = clause.lower()
            src = "compliance_elements.required_behaviors"

            if (
                "law blog" in bl
                or ("consulting" in bl and "marketing" in bl)
                or ("do not cite" in bl and "blog" in bl)
            ):
                for needle in ("law blog", "consulting website", "marketing website"):
                    if needle in course_lower:
                        _add(
                            "course.text.sources",
                            "no prohibited source wording",
                            needle,
                            "blocker",
                            (
                                f"The course text mentions '{needle}', which is a prohibited source type. "
                                "Compliance rules do not allow citing law blogs or marketing/consulting websites."
                            ),
                            src,
                            f"bad_src:{needle}",
                        )
                if re.search(r"https?://[^\s]*blog", course_lower):
                    _add(
                        "course.text.urls",
                        "no blog URLs",
                        "http…blog",
                        "blocker",
                        "A URL in the course text appears to link to a blog. Compliance rules prohibit citing blog sources.",
                        src,
                        "url_blog",
                    )
                continue

            if "cite only primary" in bl or (
                "primary regulatory" in bl and "(" in clause
            ):
                tokens = _parenthetical_tokens(clause if "(" in clause else behavior)
                if tokens:
                    hits = []
                    for tok in tokens:
                        tl = tok.strip()
                        if not tl:
                            continue
                        pat = rf"(?<![A-Za-z0-9]){re.escape(tl)}(?![A-Za-z0-9])"
                        if re.search(pat, course_lower, flags=re.IGNORECASE):
                            hits.append(tok)
                    if not hits:
                        _add(
                            "course.text.regulatory_citations",
                            "at least one listed regulator/agency token",
                            tokens[:8],
                            "warning",
                            (
                                "The course text doesn't appear to reference any of the required regulatory sources "
                                f"(expected at least one of: {tokens[:6]}…). "
                                "The rules require citing only primary regulatory sources."
                            ),
                            src,
                            f"primary:{','.join(tokens[:4])}",
                        )
                continue

            if "do not use" in bl and "'we'" in bl:
                we_n = len(re.findall(r"\bwe\b", course_lower))
                if we_n >= 3:
                    _add(
                        "course.text.organization_voice",
                        "minimal organizational 'we'",
                        we_n,
                        "warning",
                        (
                            f"The word 'we' appears {we_n} times in the course. "
                            "The compliance rules ask to avoid 'we' for organizational references — "
                            "use 'this course' instead."
                        ),
                        src,
                        "no_we_count",
                    )
                continue

            if (
                "this course" in bl
                and ("organizational" in bl or "organization" in bl)
                and "do not use" not in bl
            ):
                if "this course" not in course_lower:
                    _add(
                        "course.text.org_reference",
                        "uses 'this course' for org reference",
                        "missing",
                        "warning",
                        "The phrase 'this course' was not found in the generated content. The compliance rules expect this phrasing when referring to the course organization.",
                        src,
                        "this_course_phrase",
                    )
                continue

            if (
                ("use 'we'" in bl or 'use "we"' in bl)
                and "organization" in bl
                and "do not" not in bl
            ):
                we_n = len(re.findall(r"\bwe\b", course_lower))
                if we_n == 0:
                    _add(
                        "course.text.organization_voice",
                        "organizational 'we' present",
                        0,
                        "warning",
                        "The word 'we' does not appear in the course text. The compliance rules expect 'we' to be used when referring to the organization.",
                        src,
                        "we_insurance",
                    )
                continue

            if (
                ("second-person" in bl or "second person" in bl)
                and "you" in bl
            ):
                sp = len(_SECOND_PERSON_PATTERN.findall(course_lower))
                if sp == 0:
                    _add(
                        "course.text.second_person",
                        "second-person learner address",
                        0,
                        "warning",
                        "The course text doesn't address the learner directly (no 'you', 'your', etc.). The compliance rules require a second-person voice — write as if speaking directly to the learner.",
                        src,
                        "second_person_behavior",
                    )
                continue

            if "third person" in bl and "learner" in bl:
                tp = len(_THIRD_PERSON_PRONOUN_PATTERN.findall(course_lower))
                sp = len(_SECOND_PERSON_PATTERN.findall(course_lower))
                role_title = re.search(
                    r"\b("
                    r"iar|investment adviser|registered representative|"
                    r"broker-dealer|principal|supervisor|representative"
                    r")\b",
                    course_lower,
                )
                if tp == 0 and not role_title:
                    _add(
                        "course.text.third_person",
                        "third-person or role-title learner references",
                        0,
                        "warning",
                        (
                            "The course text has very few third-person references (he, she, they) or role titles "
                            "(e.g. IAR, registered representative). "
                            "The compliance rules expect a third-person writing style for this course type."
                        ),
                        src,
                        "third_person_behavior",
                    )
                if sp >= 5:
                    _add(
                        "course.text.second_person",
                        "limited second-person",
                        sp,
                        "warning",
                        (
                            f"The course text uses second-person forms ('you', 'your') {sp} times, "
                            "but this course type requires third-person voice. "
                            "Please revise to use 'the learner', role titles, or third-person pronouns."
                        ),
                        src,
                        "third_vs_second",
                    )
                continue

            if "claimants" in bl and "they" in bl:
                if "they" not in course_lower and "claimant" not in course_lower:
                    _add(
                        "course.text.client_refs",
                        "they / claimant wording",
                        "absent",
                        "info",
                        "Little or no use of 'they' or 'claimant' for client references. This is optional — just a note.",
                        src,
                        "they_claimant",
                    )
                continue

            if (
                "state insurance" in bl
                or "departments of insurance" in bl
                or "department of insurance" in bl
            ):
                markers = (
                    "department of insurance",
                    "division of insurance",
                    "insurance regulator",
                    "state regulator",
                    "state department",
                    "doi ",
                    "commissioner of insurance",
                )
                if not any(m in course_lower for m in markers):
                    _add(
                        "course.text.state_insurance",
                        "state insurance regulator anchoring",
                        "no marker",
                        "warning",
                        (
                            "The course text doesn't appear to reference a state insurance department or regulator. "
                            "The compliance rules expect this course to include references to state insurance authorities "
                            "(e.g. Department of Insurance, Division of Insurance, state regulator)."
                        ),
                        src,
                        "state_doi",
                    )
                continue

            if bl.startswith("bold ") or "bold the first mention" in bl:
                if "**" not in course_raw:
                    _add(
                        "course.text.bold_markers",
                        "markdown bold markers for key terms",
                        "no ** in output",
                        "warning",
                        (
                            "No bold text was found in the generated content. "
                            "The compliance rules require key terms to be bolded on their first mention."
                        ),
                        src,
                        "bold_markdown_missing",
                    )
                continue

            tone_hints = (
                "neutral explanations",
                "financial advice tone",
                "informational",
                "unsupported claims",
            )
            if any(h in bl for h in tone_hints):
                _add(
                    "course.text.tone",
                    "manual / LLM review for tone rules",
                    clause[:80],
                    "info",
                    f"This tone rule requires manual review and cannot be checked automatically: {clause[:200]}",
                    src,
                    f"tone:{bl[:40]}",
                )
                continue

    return issues


def check_voice_pronouns(sections: list[dict], rule_pack: dict) -> list[dict]:
    """Heuristic voice check derived from style_constraints.voice."""
    issues: list[dict] = []
    voice = (rule_pack.get("style_constraints", {}) or {}).get("voice", "").lower()
    if not voice:
        return issues

    expects_second = voice.startswith("second_person") or "second_person" in voice
    expects_third = voice.startswith("third_person")

    for sec in sections:
        if sec.get("status") in ("skipped", "skipped_thin"):
            continue
        flat = _flatten_text(sec)
        if len(flat.split()) < 80:
            continue
        sid = sec.get("section_id") or sec.get("heading") or "?"

        second_hits = len(_SECOND_PERSON_PATTERN.findall(flat))
        third_hits = len(_THIRD_PERSON_PRONOUN_PATTERN.findall(flat))

        if expects_second and second_hits == 0:
            issues.append({
                "field": f"section.{sid}.voice",
                "expected": f"second-person (you/your/yourself/yours) ({voice})",
                "found": "no 2nd-person pronouns",
                "severity": "warning",
                "message": (
                    f"Section '{sec.get('heading')}' doesn't address the learner directly. "
                    "The rules require second-person voice — use 'you' and 'your' throughout."
                ),
                "rule_source": "style_constraints.voice",
            })
        if expects_third and second_hits >= 3:
            issues.append({
                "field": f"section.{sid}.voice",
                "expected": f"third-person voice ({voice}); minimal 2nd person",
                "found": f"{second_hits} second-person token(s)",
                "severity": "warning",
                "message": (
                    f"Section '{sec.get('heading')}' uses second-person forms ('you', 'your') {second_hits} times, "
                    "but this course type requires third-person voice. "
                    "Please revise to use third-person references (he/she/they) or role titles."
                ),
                "rule_source": "style_constraints.voice",
            })

    return issues


def check_regulatory_mode(rule_pack: dict) -> list[dict]:
    """Emit informational note about the regulatory_mode in effect."""
    issues: list[dict] = []
    mode = (rule_pack.get("compliance_elements", {}) or {}).get("regulatory_mode")
    if not mode:
        return issues
    issues.append({
        "field": "regulatory_mode",
        "expected": "informational",
        "found": mode,
        "severity": "info",
        "message": f"This course is running in '{mode}' compliance mode. This is just a note — no action needed.",
        "rule_source": "compliance_elements.regulatory_mode",
    })
    return issues


# ---------------------------------------------------------------------------
# Content structure checks
# ---------------------------------------------------------------------------

def check_intro_section(sections: list[dict], rule_pack: dict) -> list[dict]:
    """content_rules.require_intro_section: first L1 lesson must look like an introduction."""
    issues: list[dict] = []
    if not (rule_pack.get("content_rules", {}) or {}).get("require_intro_section"):
        return issues
    lessons = _l1_lessons(sections)
    if not lessons:
        return issues
    first = lessons[0]
    heading = (first.get("heading") or "").lower()
    if not any(tok in heading for tok in ("intro", "overview", "welcome")):
        issues.append({
            "field": f"section.{first.get('section_id') or first.get('heading')}.heading",
            "expected": "first lesson reads as an introduction",
            "found": first.get("heading"),
            "severity": "warning",
            "message": (
                f"The first section '{first.get('heading')}' doesn't look like an introduction. "
                "It should have a title like 'Introduction', 'Overview', or 'Welcome'."
            ),
            "rule_source": "content_rules.require_intro_section",
        })
    return issues


def check_los_in_first_section(sections: list[dict], rule_pack: dict) -> list[dict]:
    """content_rules.require_learning_objectives_in_first_section."""
    issues: list[dict] = []
    if not (rule_pack.get("content_rules", {}) or {}).get(
        "require_learning_objectives_in_first_section"
    ):
        return issues
    lessons = _l1_lessons(sections)
    if not lessons:
        return issues
    first = lessons[0]
    flat = _flatten_text(first)
    if "learning objective" not in flat and "objectives:" not in flat and "objectives\n" not in flat:
        issues.append({
            "field": f"section.{first.get('section_id') or first.get('heading')}.body_paragraphs",
            "expected": "learning objectives listed in first section",
            "found": "no LO mention",
            "severity": "warning",
            "message": (
                f"The first section '{first.get('heading')}' doesn't appear to list the learning objectives. "
                "The rules require learning objectives to be presented in the first section of the course."
            ),
            "rule_source": "content_rules.require_learning_objectives_in_first_section",
        })
    return issues


def check_summary_section(sections: list[dict], rule_pack: dict) -> list[dict]:
    """content_rules.require_expanded_summary_section: last L1 lesson must look like a summary."""
    issues: list[dict] = []
    if not (rule_pack.get("content_rules", {}) or {}).get("require_expanded_summary_section"):
        return issues
    lessons = _l1_lessons(sections)
    if not lessons:
        return issues
    last = lessons[-1]
    heading = (last.get("heading") or "").lower()
    if not any(tok in heading for tok in ("summary", "conclusion", "review", "recap", "wrap")):
        issues.append({
            "field": f"section.{last.get('section_id') or last.get('heading')}.heading",
            "expected": "final lesson reads as a summary/conclusion",
            "found": last.get("heading"),
            "severity": "warning",
            "message": (
                f"The last section '{last.get('heading')}' doesn't look like a summary or conclusion. "
                "It should have a title like 'Summary', 'Conclusion', 'Review', or 'Recap'."
            ),
            "rule_source": "content_rules.require_expanded_summary_section",
        })
    return issues


def check_callouts_per_section(sections: list[dict], rule_pack: dict) -> list[dict]:
    """content_rules.require_callouts_per_section: [min, max] important_callout per section."""
    issues: list[dict] = []
    rng = _range_pair(
        (rule_pack.get("content_rules", {}) or {}).get("require_callouts_per_section")
    )
    if not rng:
        return issues
    lo, hi = rng
    for sec in sections:
        if sec.get("status") in ("skipped", "skipped_thin"):
            continue
        if sec.get("is_knowledge_check"):
            continue
        if sec.get("level") not in (1, 2):
            continue
        cnt = _section_block_counts(sec).get("important_callout", 0)
        sid = sec.get("section_id") or sec.get("heading") or "?"
        if cnt < lo:
            issues.append({
                "field": f"section.{sid}.callouts",
                "expected": f">= {lo} important_callout block(s)",
                "found": cnt,
                "severity": "warning",
                "message": (
                    f"Section '{sec.get('heading')}' has {cnt} callout box(es), but the rules require "
                    f"between {lo} and {hi} callout boxes per section."
                ),
                "rule_source": "content_rules.require_callouts_per_section",
            })
        elif cnt > hi:
            issues.append({
                "field": f"section.{sid}.callouts",
                "expected": f"<= {hi} important_callout block(s)",
                "found": cnt,
                "severity": "warning",
                "message": (
                    f"Section '{sec.get('heading')}' has {cnt} callout box(es), but the rules allow "
                    f"a maximum of {hi} per section."
                ),
                "rule_source": "content_rules.require_callouts_per_section",
            })
    return issues


def check_examples_per_section(sections: list[dict], rule_pack: dict) -> list[dict]:
    """content_rules.require_examples_per_section: [min, max] examples per section (heuristic)."""
    issues: list[dict] = []
    rng = _range_pair(
        (rule_pack.get("content_rules", {}) or {}).get("require_examples_per_section")
    )
    if not rng:
        return issues
    lo, _hi = rng
    for sec in sections:
        if sec.get("status") in ("skipped", "skipped_thin"):
            continue
        if sec.get("is_knowledge_check"):
            continue
        if sec.get("level") not in (1, 2):
            continue
        flat = _flatten_text(sec)
        markers = ("for example", "e.g.", "example:", "for instance", "such as")
        cnt = sum(flat.count(m) for m in markers)
        sid = sec.get("section_id") or sec.get("heading") or "?"
        if cnt < lo:
            issues.append({
                "field": f"section.{sid}.examples",
                "expected": f">= {lo} example(s)",
                "found": cnt,
                "severity": "warning",
                "message": (
                    f"Section '{sec.get('heading')}' appears to have only {cnt} example(s). "
                    f"The rules require at least {lo} example(s) per section."
                ),
                "rule_source": "content_rules.require_examples_per_section",
            })
    return issues


def check_no_duplicate_headings(sections: list[dict], rule_pack: dict) -> list[dict]:
    """content_rules.no_duplicate_concepts_across_sections (heading-level proxy)."""
    issues: list[dict] = []
    if not (rule_pack.get("content_rules", {}) or {}).get(
        "no_duplicate_concepts_across_sections"
    ):
        return issues
    seen: dict[str, str] = {}
    for sec in sections:
        if sec.get("status") in ("skipped", "skipped_thin"):
            continue
        if sec.get("is_knowledge_check"):
            continue
        h = (sec.get("heading") or "").strip().lower()
        if not h:
            continue
        if h in seen and seen[h] != sec.get("section_id"):
            issues.append({
                "field": f"section.{sec.get('section_id') or sec.get('heading')}.heading",
                "expected": "unique heading across course",
                "found": sec.get("heading"),
                "severity": "warning",
                "message": (
                    f"The heading '{sec.get('heading')}' appears in more than one section. "
                    "Each section should have a unique title to avoid confusion in the final document."
                ),
                "rule_source": "content_rules.no_duplicate_concepts_across_sections",
            })
        else:
            seen[h] = sec.get("section_id") or sec.get("heading") or h
    return issues


# ---------------------------------------------------------------------------
# LO coverage check (post-generation)
# ---------------------------------------------------------------------------

def check_lo_coverage(sections: list[dict], shared_state: dict, rule_pack: dict) -> list[dict]:
    """Every learning objective must be mapped to at least one generated section."""
    issues: list[dict] = []
    if not (rule_pack.get("content_rules", {}) or {}).get("must_map_to_learning_objectives", True):
        return issues

    los = (shared_state.get("extracted_inputs", {}) or {}).get("learning_objectives") or []
    if not los:
        return issues

    mapped: set[int] = set()
    for sec in sections:
        for idx in sec.get("maps_to_objectives", []) or []:
            if isinstance(idx, int):
                mapped.add(idx)

    unmapped = [i for i in range(len(los)) if i not in mapped]
    if unmapped:
        issues.append({
            "field": "learning_objectives_coverage",
            "expected": f"all {len(los)} LOs mapped",
            "found": f"{len(unmapped)} unmapped (indices {unmapped})",
            "severity": "warning",
            "message": (
                f"The generated course content does not appear to cover learning objective(s) "
                f"{[i + 1 for i in unmapped]}. Please ensure all learning objectives are addressed."
            ),
            "rule_source": "content_rules.must_map_to_learning_objectives",
        })
    return issues
