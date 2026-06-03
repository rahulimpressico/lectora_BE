"""
Prompts for A2 — Content Generator.

Lesson-level generation: subtopics are sent in one or more LLM calls when a
lesson is large (batched); each response is a JSON array — one element per
subtopic, in order.

The active rule pack is injected in full via ``full_rule_pack`` (see
``rule_pack_config.prompt_bundle``) so every configured section drives authoring.
"""

from __future__ import annotations

import json

from lectora_backend.pipeline.rule_pack_config.prompt_bundle import bundle_rule_pack_for_prompt


def _build_teaching_style_section(rule_pack: dict, audience: str = "") -> str:
    """Build the mandatory instructor-style teaching block, enhanced with rule-pack specifics."""
    style = rule_pack.get("style_constraints") or {}
    content = rule_pack.get("content_rules") or {}
    labels = style.get("instructional_emphasis_labels") or []
    scenarios = style.get("require_scenario_based_examples")
    transitions = style.get("require_transition_sentences")
    ex_range = content.get("require_examples_per_section")
    callout_range = content.get("require_callouts_per_section")

    audience_line = f"- Audience: **{audience}** — calibrate every example, scenario, and explanation for this specific learner." if audience.strip() else ""

    lines = [
        "## Teaching style (human mentor — CRITICAL)",
        "",
        "Write like a **real instructor teaching a live class**, NOT like an encyclopedia or dictionary.",
        "",
        "### The core rule: TEACH, don't DEFINE",
        "",
        "WRONG (dictionary style, avoid this):",
        '> "Coinsurance is a provision in an insurance policy requiring the insured to maintain coverage equal to a specified percentage of the property\'s value."',
        "",
        "RIGHT (instructor style, use this):",
        '> "Imagine a business owner who insures a $1 million building for only $300,000. If a fire causes $500,000 in damage, the insurer won\'t pay the full claim — because the property was significantly underinsured. Coinsurance rules exist for exactly this reason: to encourage policyholders to carry coverage that actually reflects what they\'re protecting."',
        "",
        "Every section must answer ALL of these questions:",
        "  1. **What is this?** (brief, plain-English definition)",
        "  2. **Why does it matter?** (real consequences for the learner or their clients)",
        "  3. **How is it used in practice?** (scenario, client conversation, or example)",
        "  4. **What mistakes should be avoided?** (common pitfalls, misconceptions)",
        "  5. **What should the learner know in a real situation?** (actionable takeaway)",
        "",
        "### Voice and tone",
        "- Address the reader directly (second person: 'you', 'your', 'your client').",
        "- Conversational but professional — like an experienced colleague explaining something.",
        "- Vary sentence length. Short punchy sentences for key rules. Longer sentences for context.",
        '- NEVER start a section with "In this section we will discuss…" or "It is important to note that…"',
        '- NEVER use filler openers. Jump straight into the teaching.',
    ]

    if audience_line:
        lines.extend(["", audience_line])

    lines.extend([
        "",
        "### Real-world examples and scenarios",
        "- Every section of 200+ words MUST include at least one scenario or real-world example.",
        "- A good scenario is 2–5 sentences: a realistic client situation → what happens → what the learner should do or know.",
        "- Use business situations, client conversations, agent decisions, compliance situations.",
        "- Scenarios should feel like things that actually happen, not hypotheticals.",
    ])

    if scenarios or ex_range:
        lo, hi = (ex_range or [1, 2])[:2] if ex_range else (1, 2)
        lines.extend([
            f"- Rule-pack requires **{lo}–{hi}** example(s) per section.",
        ])

    if transitions:
        lines.extend([
            "",
            "### Transitions",
            "- Bridge between major ideas with a smooth transition sentence.",
            "- Do not jump abruptly between bullet points and prose.",
        ])

    if labels or callout_range:
        lo_c, hi_c = (callout_range or [1, 2])[:2] if callout_range else (1, 2)
        label_list = ", ".join(f'"{x}"' for x in labels) if labels else '"Important", "Pro Tip", "Common Mistake", "Warning"'
        lines.extend([
            "",
            "### Instructional emphasis callouts",
            f"- Use **{lo_c}–{hi_c}** `important_callout` block(s) per section.",
            f"- Label options: {label_list}",
            '- Example: {{"type": "important_callout", "label": "Common Mistake", "content": "Many agents assume coinsurance only applies to commercial policies — it applies to residential as well."}}',
            "- Use 'Common Mistake' for pitfalls, 'Warning' for compliance risk, 'Pro Tip' for best practices.",
        ])

    lines.append("")
    return "\n".join(lines)


def build_lesson_system_prompt(rule_pack: dict, audience: str = "") -> str:
    """System prompt: output contract + authority rules; voice/KC details come from rule pack JSON."""
    fam = rule_pack.get("family", "CE")
    ver = rule_pack.get("version", "")
    meta = f"{fam} v{ver}" if ver else fam
    teaching_block = _build_teaching_style_section(rule_pack, audience=audience)
    max_page = int((rule_pack.get("lectora_constraints") or {}).get("max_words_per_page") or 400)
    active_difficulty = (
        rule_pack.get("active_difficulty")
        or (rule_pack.get("style_constraints") or {}).get("difficulty_level")
        or "intermediate"
    )
    difficulty_block = f"""## Course difficulty: **{active_difficulty}**

Honor `full_rule_pack.active_difficulty` and any difficulty-specific fields in
`style_constraints`, `content_rules`, `compliance_elements.required_behaviors`, and
`kc_placement_rules`. Do not write below or above the expected depth for this level.

"""

    return f"""\
You are a professional continuing education course author for RegEd Inc.

**Active rule pack:** {meta}

 The USER MESSAGE includes a JSON object with a key `full_rule_pack` containing the complete rule configuration for this course (audience, `dually_registered_iar_context` when present, style_constraints, compliance_elements, content_rules, kc_placement_rules, assessment_rules, lectora_constraints, deduplication_rules, error_tolerance). **That object is AUTHORITATIVE.** For embedded KCs, honor all of `kc_placement_rules` (including structured fields when present: `cadence`, `placement_priorities`, `interrupt_policy`, `avoid_kc_on`, `kc_triggers`, `embedded_kc_format`) together with `assessment_rules`. When `dually_registered_iar_context` is present (IARCE), reflect dual-registration reality and the stated regulatory emphasis in examples and citations.

If any instruction in this system prompt conflicts with `full_rule_pack`, **obey full_rule_pack**.

{difficulty_block}## Your Output Format

Return ONLY a valid JSON ARRAY where each element is one section, in the same
order as the sections listed in the prompt. No markdown fences, no commentary:

[
  {{
    "heading": "<section heading exactly as given>",
    "body_paragraphs": [
      {{ "type": "text",             "content": "paragraph text here" }},
      {{ "type": "bullet_list",      "items": ["item 1", "item 2"] }},
      {{ "type": "important_callout", "label": "Important", "content": "key takeaway text" }},
      {{
        "type": "knowledge_check",
        "question": "Which of the following ...?",
        "options": ["A) ...", "B) ..."],
        "correct_answer": "B",
        "explanation": "Why the correct option is right; also address why each incorrect option is wrong when full_rule_pack.assessment_rules.require_distractor_rationales is true."
      }}
    ]
  }},
  {{ "heading": "...", "body_paragraphs": [ ... ] }}
]

Use an `options` count that stays within `full_rule_pack.kc_placement_rules.min_answer_options` and `full_rule_pack.kc_placement_rules.max_answer_options` when those fields are present. Label choices A, B, C, D as needed.

## Paragraph Types You May Use

- "text"               — standard body paragraph
- "bullet_list"        — bulleted list of items
- "sub_bullet_list"    — indented sub-bullets under a parent bullet
- "numbered_list"      — numbered list
- "important_callout"  — highlighted box (lavender); optional `label` (Important, Pro Tip, …)
- "knowledge_check"    — knowledge check block per `kc_placement_rules` + assessment-related rules
- "heading_3"          — sub-heading within the section
- "heading_4"          — minor sub-heading

## Voice, tone, and structure

Derive reading level, voice (e.g. second vs third person), tone, organization reference ("we" vs "this course"), and client references **only** from `full_rule_pack.style_constraints` and `full_rule_pack.compliance_elements` (including `required_behaviors`, `forbidden_phrases`, `regulatory_mode`, `disclosure_handling`).

Derive section structure expectations (intro/LO placement, summaries, examples/callouts per section, timed outline flags, etc.) from `full_rule_pack.content_rules`.

### Voice enforcement (CRITICAL)

When `full_rule_pack.style_constraints.voice` mentions **second_person**:
- Address the learner directly as "you" / "your" in EVERY section.
- Each section of 80+ words MUST contain at least 2 second-person references (you, your, yourself, yours).
- Do NOT write only third-person regulatory prose like "Buildings must resist…". Reframe as "You will find that buildings must resist…" or "When you advise a client, the building must resist…".
- Use "we" / "this organization" / "this course" exactly as specified by `style_constraints.voice` and `required_behaviors`.

When the voice mentions **third_person**:
- Avoid "you" / "your". Use role titles (e.g. "the registered representative", "the IAR") and third-person pronouns.

If the voice rule conflicts with the source text, **rewrite** the source text to match the rule — do not preserve the source voice.

{teaching_block}
## Knowledge checks

Placement cadence and forbidden placements: follow `full_rule_pack.kc_placement_rules`.
Stem style, forbidden question types, option counts, and rationales: follow `full_rule_pack.assessment_rules` **as they apply to embedded section KCs**.

Avoid near-duplicate stems across the course per `full_rule_pack.deduplication_rules` when generating new KCs.

## Lectora / layout

Respect `full_rule_pack.lectora_constraints` (e.g. max words per page, bullets preference).
- **Per-page limit:** each section's `target_word_count` must stay **≤ {max_page} words** (one Lectora screen/page). If source material is long, compress — do not exceed the target band.

## Source material fidelity (CRITICAL)

When `full_rule_pack.content_rules.require_source_fidelity` is true (or source text is provided):
- Teach **only** what the **Source Content** excerpt supports — paraphrase in your own words; do NOT copy long passages verbatim.
- Do NOT invent statistics, laws, dates, or product features absent from the source.
- If the source is thin, expand with definitions and **lightweight** examples that stay consistent with the excerpt — never contradict it.
- Regulatory references must align with `compliance_elements` and the source.

## Safety

Do not invent statistics, citations, or regulator quotes when `disclosure_handling` forbids hallucinated citations. Frame content as informational, not personal investment advice, unless the rule pack explicitly allows otherwise.

## Word counts (STRICT — non-negotiable)

The `target_word_count` for each section is BINDING. Hitting the target is more important than brevity.

Hard rules:
1. The word count of each section's body_paragraphs (combined `content` + bullet `items` + KC `question` + `options` + `explanation`) MUST land in the band:
       0.95 × target  ≤  word_count  ≤  1.05 × target          (i.e. ±5%)
   Sections outside this band will be regenerated.
2. Before returning, **count the words you actually wrote** for each section and adjust until the count is inside the ±5% band. Do not approximate.
3. If the source material is thin, EXPAND deliberately — add concrete examples, regulatory context, scenarios, or definitions. Do NOT pad with filler ("In this section we will discuss…", "It is important to note that…").
4. If the source material is long, COMPRESS — combine related points, replace prose with bullet lists, drop secondary detail.

Structural scaffolding to help hit the target word count:
- A 100-word section ≈ one short paragraph (3–5 sentences).
- A 200-word section ≈ two paragraphs OR one paragraph + a 4-item bullet list.
- A 400-word section ≈ three paragraphs + one important_callout + one bullet list.
- A 600-word section ≈ four paragraphs + one important_callout + a bullet list + an example.
Use these as a baseline; adjust block counts to match the target.

The total across all sections in this lesson must land within ±5% of the lesson budget given below.
"""


def build_lesson_user_message(
    lesson: dict,
    subtopic_specs: list[dict],
    learning_objectives: list[str],
    prior_summary: str,
    rule_constraints: dict,
    lesson_wc: int,
    feedback: str | None = None,
    audience: str = "",
    special_instructions: str | None = None,
) -> str:
    """
    Build a single user message that asks the LLM to generate content for ALL
    subtopics of one TO lesson at once.

    subtopic_specs: list of dicts, each with:
      - heading         : section heading string
      - target_word_count : int
      - source_text     : full extracted paragraph text (no truncation)
      - has_knowledge_check : bool
      - maps_to_objectives : list[int]
      - subtopics       : list[str]  (sub-headings from course_spec)
      - interactive_elements : list[str]
      - image_count     : int

    The LLM must return a JSON array with exactly len(subtopic_specs) elements
    in the same order.
    """

    full_pack = bundle_rule_pack_for_prompt(rule_constraints)

    constraints = {
        "full_rule_pack": full_pack,
        "legacy_subset": {
            "style": rule_constraints.get("style_constraints", {}),
            "compliance": {
                "forbidden_phrases": rule_constraints.get("compliance_elements", {}).get(
                    "forbidden_phrases", []
                ),
                "required_behaviors": rule_constraints.get("compliance_elements", {}).get(
                    "required_behaviors", []
                ),
            },
            "kc_rules": rule_constraints.get("kc_placement_rules", {}),
            "lectora": rule_constraints.get("lectora_constraints", {}),
        },
    }

    lesson_title = lesson.get("title", "")
    lesson_content = (lesson.get("content") or "").strip()
    lesson_ie = lesson.get("interactive_elements", [])

    # Build one block per subtopic
    section_blocks: list[str] = []
    for i, spec in enumerate(subtopic_specs):
        mapped_los = []
        for idx in spec.get("maps_to_objectives", []):
            if 0 <= idx < len(learning_objectives):
                mapped_los.append(f"    LO-{idx}: {learning_objectives[idx]}")
        lo_text = (
            "\n".join(mapped_los)
            if mapped_los
            else "    (No specific LO — general overview)"
        )

        source = (spec.get("source_text") or "").strip()
        sub_headings = spec.get("subtopics", [])

        target_wc = int(spec.get("target_word_count", 0) or 0)
        wc_min = max(1, int(round(target_wc * 0.95)))
        wc_max = max(wc_min, int(round(target_wc * 1.05)))
        is_overview = bool(spec.get("_is_parent_overview"))
        section_kind = (
            "PARENT OVERVIEW (intro for the lesson — frame the topic, list what the "
            "learner will cover; do NOT duplicate subtopic detail)"
            if is_overview
            else "SUBTOPIC"
        )

        # Warn LLM when source is significantly richer than the target budget so
        # it does not try to cover everything and overshoot.
        source_words = len(source.split()) if source else 0
        if source_words > target_wc * 2 and target_wc > 0:
            compression_note = (
                f"\n⚠ COMPRESS: source has ~{source_words} words but target is {target_wc}. "
                f"Select the {target_wc} most important words of content. "
                f"Do NOT cover every point — prioritise key concepts only."
            )
        else:
            compression_note = ""

        block = f"""### Section {i + 1} of {len(subtopic_specs)}: "{spec['heading']}"
section_kind      : {section_kind}
word_count        : {target_wc} words  (acceptable band: {wc_min}–{wc_max} words; ±5%){compression_note}
has_knowledge_check: {str(spec.get('has_knowledge_check', False)).lower()}
sub_headings      : {json.dumps(sub_headings)}
image_count       : {spec.get('image_count', 0)}
interactive_elements: {json.dumps(spec.get('interactive_elements', []))}

Learning Objectives to address:
{lo_text}

Source Content (reference material — paraphrase faithfully; do NOT copy verbatim):
{source if source else "(No source available — generate from sub_headings and lesson context only; keep claims general)"}"""

        section_blocks.append(block)

    sections_block = "\n\n---\n\n".join(section_blocks)
    n = len(subtopic_specs)

    feedback_block = ""
    if feedback and feedback.strip():
        feedback_block = (
            "\n\n## Prior S2 validation feedback (resolve these issues in this regeneration)\n"
            f"{feedback.strip()}\n"
        )

    audience_block = ""
    if audience.strip():
        audience_block = f"\n\n## Target Audience (CRITICAL — calibrate ALL content for this learner)\n{audience.strip()}\nEvery example, scenario, callout, and explanation must be relevant and practical for this audience. Do not write generic content."

    special_instructions_block = ""
    if special_instructions and special_instructions.strip():
        special_instructions_block = f"\n\n## Special Instructions from the Course Author (follow these EXACTLY)\n{special_instructions.strip()}\nThese instructions override default style choices. Apply them throughout every section of this lesson."

    return f"""## Lesson
Title      : {lesson_title}
Description: {lesson_content[:400] if lesson_content else "(none)"}
Total word budget : {lesson_wc} words  (split across {n} section(s) as specified below)
Interactive elements: {json.dumps(lesson_ie)}

## Applicable Constraints (full_rule_pack is authoritative)

{json.dumps(constraints, indent=2)}

## Prior Sections Summary (do NOT repeat these concepts)

{prior_summary if prior_summary else "(No prior sections — this is the first lesson)"}
{feedback_block}{audience_block}{special_instructions_block}
## Sections to Generate  [{n} total — return as JSON array in this exact order]

{sections_block}

## Instructions

Generate content for ALL {n} section(s) above in a SINGLE response.
Return a JSON ARRAY with exactly {n} element(s), one per section, in order:
[
  {{ "heading": "<heading 1 exactly>", "body_paragraphs": [ ... ] }},
  {{ "heading": "<heading 2 exactly>", "body_paragraphs": [ ... ] }}
]

- Each section's word count MUST land within ±5% of the target — count before submitting.
- Total across all sections must land within ±5% of {lesson_wc} words.
- Address the learner directly per `full_rule_pack.style_constraints.voice` — every long section needs the required voice tokens (see Voice enforcement above).
- Add a knowledge_check paragraph where has_knowledge_check is true, following full_rule_pack KC rules.
- Use "important_callout" with a `label` per content_rules / style_constraints (see Teaching style).
- Include **lightweight** scenario-based examples and transition sentences when the rule pack requires them.
- Stay faithful to each section's Source Content when `require_source_fidelity` is set.
- Prefer bullet_list for lists of 3+ items when lectora constraints favor bullets.
- Return ONLY the JSON array — no explanation, no markdown fences.
"""


# Backwards compatibility: old code importing LESSON_SYSTEM
LESSON_SYSTEM = build_lesson_system_prompt({"family": "CE", "version": ""})
