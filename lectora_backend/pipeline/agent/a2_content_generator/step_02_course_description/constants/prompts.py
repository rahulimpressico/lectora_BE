"""
LLM prompt for the study guide **course description** (OVERVIEW section in DOCX).

User message: course title + learning objectives + up to 8000 words of raw document content.
"""

_CONTENT_SAMPLE_MAX_WORDS = 8000


COURSE_DESCRIPTION_SYSTEM_PROMPT = """You are a senior instructional designer writing the OVERVIEW description (section 1.0) for a professional continuing education (CE) study guide aimed at **students**.

You will receive:
1. The **course title**
2. The **learning objectives** (what learners will be able to do after completing the course)
3. A long **excerpt** from the raw source study material (up to 8000 words)

Use all three inputs together to write a complete, accurate overview. Infer audience, subject matter, and progression **only** from these inputs. Do not assume facts not present in the material.

Your job is to write a **complete** overview in your own words — not a paragraph-by-paragraph summary.

Cover all of the following:
1. What the course is about — subject and implied audience based on the title and content.
2. Why the topic matters in professional practice (as supported by the material).
3. How the material progresses — infer themes and sequence from the excerpt (e.g., foundations → application). Describe in prose; do NOT produce a table-of-contents-style list.
4. What the learner gains — directly tied to the stated learning objectives.

Writing guidance:
- Connected narrative for **students** — clear, welcoming, CE-appropriate tone (not stiff policy memo).
- Speak to what the student will learn and why it matters for their role.
- Paraphrase themes; do not quote long passages.
- The learning objectives should inform the final paragraph (what value the learner takes away).

Strict constraints:
- Stay faithful to the **source excerpt** — paraphrase only; do not invent facts.
- Do NOT include bullets, numbering, or headings in the output.
- Do NOT invent laws, statistics, or guarantees not supported by the excerpt.
- Avoid vacuous filler.

Output requirements:
- Plain text only
- 1 paragraph only
- **Exactly around 120 words** — no shorter than 110, no longer than 130
- Tight, complete sentences; stop once you reach ~120 words
"""


def _truncate_sample(text: str, max_words: int = _CONTENT_SAMPLE_MAX_WORDS) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    words = t.split()
    if len(words) <= max_words:
        return t
    return " ".join(words[:max_words]).rstrip() + "\n[…excerpt truncated at word limit…]"


def build_course_description_user_message(
    course_title: str,
    content_sample: str,
    *,
    learning_objectives: list[str] | None = None,
) -> str:
    """Course title + learning objectives + truncated raw content sample."""

    parts: list[str] = [
        f"Course title:\n{course_title.strip()}\n\n",
    ]

    los = [lo.strip() for lo in (learning_objectives or []) if lo and lo.strip()]
    if los:
        lo_lines = "\n".join(f"  {i + 1}. {lo}" for i, lo in enumerate(los))
        parts.append(f"Learning objectives:\n{lo_lines}\n\n")

    excerpt = _truncate_sample(content_sample)
    if excerpt:
        parts.append(
            "Raw course content (up to 8000 words from source document — "
            "paraphrase themes; do not quote at length):\n"
            f"{excerpt}\n\n"
        )

    parts.append(
        "Write the course overview now as a single cohesive paragraph. "
        "**Target exactly ~120 words (110–130 range).** "
        "The reader should understand what this offering is, how the material builds, "
        "and what professional value it supports — directly grounded in the learning objectives above. "
        "Stop writing once you reach approximately 120 words."
    )

    return "".join(parts)
