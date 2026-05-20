"""
LLM prompt for the study guide **Conclusion** section (final section in DOCX).
"""

from lectora_backend.pipeline.agent.a2_content_generator.utils.source_chunker import (
    build_prior_summary,
)

_CONTENT_SAMPLE_MAX_WORDS = 4000
_TARGET_CONCLUSION_WORDS = 250


CONCLUSION_SYSTEM_PROMPT = """You are a senior instructional designer writing the final **Conclusion** section for a CE study guide aimed at **students**.

You will receive:
1. The **course title**
2. The **learning objectives**
3. A **course outline** (headings of sections already written)
4. An **excerpt** from the source study material

Write a closing section that:
1. Recaps what the student learned — tied directly to the learning objectives (do not invent new objectives).
2. Reinforces why the material matters for professional practice (only as supported by the excerpt).
3. Ends with an encouraging, forward-looking sentence for the student.

Writing guidance:
- Student-friendly, mentor tone — conversational but professional.
- **Ground every point in the source excerpt and outline** — paraphrase; do not invent laws, statistics, or guarantees.
- Include **one brief lightweight scenario** (2–4 sentences) that shows how a student applies the main ideas.
- Use smooth transition phrasing between recap themes.

Strict constraints:
- Plain text only (no bullets, numbering, or headings in the output).
- **1–2 paragraphs**, **200–300 words** total.
- Do NOT repeat the OVERVIEW introduction verbatim.
"""


def build_conclusion_user_message(
    course_title: str,
    content_sample: str,
    *,
    learning_objectives: list[str] | None = None,
    generated_sections: list[dict] | None = None,
) -> str:
    title = (course_title or "").strip() or "Untitled Course"
    parts = [f"Course title:\n{title}\n\n"]

    los = [lo.strip() for lo in (learning_objectives or []) if lo and lo.strip()]
    if los:
        lo_lines = "\n".join(f"  {i + 1}. {lo}" for i, lo in enumerate(los))
        parts.append(f"Learning objectives to recap:\n{lo_lines}\n\n")

    outline = build_prior_summary(generated_sections or [], max_chars=2000)
    parts.append(f"Course sections already covered:\n{outline}\n\n")

    excerpt = (content_sample or "").strip()
    if excerpt:
        words = excerpt.split()
        if len(words) > _CONTENT_SAMPLE_MAX_WORDS:
            excerpt = " ".join(words[:_CONTENT_SAMPLE_MAX_WORDS]) + "\n[…excerpt truncated…]"
        parts.append(
            "Source reference material (stay faithful — paraphrase themes only):\n"
            f"{excerpt}\n\n"
        )

    parts.append(
        f"Write the Conclusion now as 1–2 paragraphs (~{_TARGET_CONCLUSION_WORDS} words). "
        "Recap objectives, include one lightweight scenario, and close for the student."
    )
    return "".join(parts)
