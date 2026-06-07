"""
LLM classification prompt for A0 — Request Synthesizer.
"""

import json

from lectora_backend.pipeline.rule_pack_config.timed_outline import TO_outline_format

CLASSIFICATION_PROMPT = """You are a course-classification expert for a regulated professional-education platform.
You will receive content signals from one or more source documents.
Your sole task: classify this course into EXACTLY ONE of the three rule families below.

═══════════════════════════════════════════════════════════
RULE FAMILY DEFINITIONS
(read these carefully before classifying — families have overlapping vocabulary)
═══════════════════════════════════════════════════════════

insurance_ce — Insurance Continuing Education
  Governed by: State insurance regulatory departments (NAIC, state DOIs)
  Audience:    State-licensed insurance producers, agents, adjusters, back-office staff

  CHOOSE insurance_ce when the content predominantly covers:
  • Property & Casualty insurance: auto, homeowners, commercial property, liability, flood (NFIP/Wright Flood), workers comp, inland marine
  • Life insurance: term, whole life, universal life, variable life
  • Health insurance: individual/group health plans, HMO/PPO, Medicare supplements, long-term care, disability income
  • Employer-sponsored benefits / employer health plans (ACA, COBRA, HIPAA, ERISA) when framed as insurance product knowledge
  • Annuities when the course focuses on insurance licensing, suitability for insurance agents, or state-regulated annuity products
  • Insurance producer licensing, CE credits, state-specific rules (e.g. Washington LTC, Louisiana flood)
  • Coverage, premiums, underwriting, endorsements, policy exclusions, claims handling
  • NAIC guidelines, state DOI regulations, Gramm-Leach-Bliley from an insurance perspective

  KEY SIGNALS: "licensed agent", "insurance producer", "policy", "coverage", "premium",
    "NFIP", "flood zone", "P&C", "homeowners", "workers compensation", "state DOI",
    "CE credit", "state insurance department", "continuing education hours"

  DISTINGUISH FROM iarce: Insurance CE covers insurance products and state licensing.
    If annuities appear alongside securities regulations or FINRA — lean iarce.
    If employer health benefits appear alongside fiduciary / investment concepts — consider iarce.

iarce — Investment Adviser Representative Continuing Education (Ethics & Professional Responsibility)
  Governed by: NASAA / SEC (state and federal securities law)
  Audience:    Investment Adviser Representatives (IARs), registered investment advisers, dually-registered reps

  CHOOSE iarce when the content predominantly covers:
  • Investment adviser regulations: fiduciary duty, RIA registration, Form ADV, investment advisory agreements
  • Ethics and professional responsibility for IARs and advisers
  • Securities law: Investment Advisers Act 1940, Securities Act 1933/1934
  • Behavioral finance, investor psychology, suitability / best-interest standards in investment context
  • Portfolio management, asset allocation, investment strategies as taught to advisers
  • Variable annuities / variable life from a securities regulation angle (not insurance licensing)
  • NASAA model rules, state securities divisions
  • IAR CE requirements (NASAA Series 65/66 context)

  KEY SIGNALS: "investment adviser", "IAR", "fiduciary", "RIA", "Form ADV",
    "NASAA", "Investment Advisers Act", "securities regulations", "suitability",
    "investment advisory", "portfolio", "asset allocation", "behavioral finance"

  DISTINGUISH FROM firm_element: iarce is for IARs and investment advisers under SEC/NASAA.
    Firm Element is for FINRA-registered broker-dealer personnel under FINRA Rule 1240.
    If content covers broker-dealer supervisory procedures or FINRA registered reps — use firm_element.

firm_element — Firm Element Continuing Education
  Governed by: FINRA Rule 1240 (formerly Rule 1250)
  Audience:    Registered representatives (RRs), broker-dealer principals, supervisors, compliance officers, operations staff at FINRA-member firms

  CHOOSE firm_element when the content predominantly covers:
  • FINRA Firm Element CE requirements for registered broker-dealer personnel
  • Broker-dealer supervisory procedures, branch office supervision
  • FINRA rules and regulations: suitability, best execution, order handling, disclosure
  • Anti-money laundering (AML), Bank Secrecy Act for broker-dealers
  • Senior investor protection (Senior Safe Act)
  • Due diligence for complex / alternative investments, private placements
  • Customer account management, KYC / CIP at broker-dealers
  • Cybersecurity, data privacy in the broker-dealer context
  • Reg BI (Regulation Best Interest), Reg SP, Reg SCI
  • Estate planning or elder financial exploitation when targeting BD reps
  • Internal firm compliance policies, ethics for registered persons

  KEY SIGNALS: "FINRA", "registered representative", "broker-dealer", "Reg BI",
    "supervisory procedures", "FINRA Rule 1240", "branch manager",
    "Series 7", "annual compliance", "registered principal", "member firm",
    "AML", "Bank Secrecy Act", "due diligence"

  DISTINGUISH FROM iarce: Firm Element is FINRA/broker-dealer focused.
    iarce is SEC/NASAA/investment-adviser focused.
    If the course covers BOTH broker-dealer rules AND investment adviser rules,
    identify which regulatory framework dominates the content.

═══════════════════════════════════════════════════════════
CLASSIFICATION PROCESS — follow these steps
═══════════════════════════════════════════════════════════
1. Read ALL provided signals (titles, headings, objectives, content).
2. Identify the governing body / regulatory framework referenced most.
3. Identify the PRIMARY audience the content is written for.
4. Match to the rule family whose definition best fits both.
5. Apply disambiguation rules when vocabulary overlaps (annuities, ethics, health).
6. If genuinely ambiguous, pick the BEST fit and note it in reasoning with confidence < 0.7.

Respond with ONLY a JSON object — no markdown, no explanation:
{
  "rule_family": "<one of: insurance_ce | iarce | firm_element>",
  "confidence": <float 0.0–1.0>,
  "audience": "<specific audience this course targets>",
  "course_type": "<e.g. Insurance CE, IAR CE — Ethics, Firm Element Annual Training>",
  "category": "<specific sub-category, e.g. Property & Casualty — Flood Insurance, IAR — Behavioral Finance>",
  "topic": "<primary topic in 5–10 words>",
  "reasoning": "<2–3 sentences explaining which signals drove the classification and why alternatives were ruled out>"
}
"""


_GENERATE_TO_section_schema = {
    "title": "",
    "content": "",
    "subtopics": [],
    "word_count": "",
    "minutes": "",
    "credit_hour": "",
    "interactive_elements": [],
    "para_idx_start": None,
    "para_idx_end": None,
}

_GENERATE_TO_format = {
    "course_title": "",
    "course_id": "",
    "description": "",
    "learning_objectives": [],
    "sections": [_GENERATE_TO_section_schema],
    "totals": {"word_count": "", "minutes": "", "credit_hours": ""},
}

# ── Shared SOURCE CONTENT FORMAT block ──────────────────────────────────────
# Injected into BOTH GENERATE_TO_PROMPT and build_dynamic_to_prompt so the LLM
# always knows how to read FORMAT A (TOC) and FORMAT B (flat indexed) user messages.
_SOURCE_CONTENT_FORMAT_BLOCK = """\
═══════════════════════════════════════════════════════════
SOURCE CONTENT FORMAT
═══════════════════════════════════════════════════════════
The user message will contain ONE of two content formats:

FORMAT A — DOCUMENT TABLE OF CONTENTS WITH MAPPED SECTION CONTENT (preferred)
   Provided when the source DOCX contains an explicit Table of Contents.
   Contains two sub-blocks:

   a) TOC Hierarchy
      Each line: [L<level>] <heading_text> (para <start>–<end>)
        L1 = top-level section / chapter
        L2 = sub-section
        L3+ = deeper nesting
      This is the document's own structural intent — use it as your starting point.
      IMPORTANT: if the user message includes a "STRICT TOC TITLE LOCK MODE" block,
      that block overrides any merge/drop/title-rewrite guidance for FORMAT A.

   b) Per-Section Content
      Each section header: ### [L<level>] <title> · para <start>–<end>
      Followed by [P<N>]-prefixed paragraphs from that section's body text.

   HOW TO USE FORMAT A:
     • Map every L1 TOC entry that passes the trainer's test → one top-level section
     • Map L2 entries → subtopics of their parent L1 section
     • Deeper levels (L3+) → nested subtopic strings inside L2 where genuinely distinct
     • Merge L1 entries that cover the same concept into one section
     • Drop L1 entries that fail the trainer's test (pure background, duplicates, admin)
     • Use the para ranges shown in "## TOC Hierarchy" for para_idx_start / para_idx_end
     • Derive "content" and "subtopics" from the section's actual body paragraphs
     • Do NOT invent topics not grounded in the source

FORMAT B — FLAT INDEXED CONTENT + OPTIONAL HEADING STRUCTURE (fallback)
   Used when no TOC is present in the source document.

   a) DOCUMENT HEADING STRUCTURE (optional)
      Format: [L<level>] <heading_text>  (L1 = top-level, L2 = sub-topic)
      Treat as raw material, not final structure. Apply trainer's mindset:
        • Keep headings that represent critical, actionable knowledge
        • Merge closely related headings into one lesson
        • Cut headings that are trivial, duplicated, or purely administrative

   b) SOURCE DOCUMENT CONTENT (with paragraph indices)
      Multiple files separated by ``--- Document: <filename> ---`` headers.
      Lines prefixed with [P<N>] where N is the paragraph index in that file.
      Use indices from the matching document block for para_idx_start / para_idx_end.

COURSE TYPE CONTEXT (optional, either format)
   A domain hint (e.g. "Washington LTC Compliance"). When present:
     • Sharpen your topic selection to what matters specifically in that domain
     • Use precise domain terminology from the source\
"""

GENERATE_TO_PROMPT = f"""\
IMPORTANT: Your response MUST be a single valid JSON object ONLY.
Do NOT output markdown, headings, prose, or any text outside the JSON object.
Start your response with "{{" and end with "}}". No code fences. No explanation.

You are a seasoned industry professional and trainer with years of hands-on experience
in this field. You have taught this material to real working professionals — you know
exactly what trips students up, what they actually use on the job, and what is merely
background noise in a textbook.

Your task is to design a Timed Outline (TO) for an eLearning course built from one or
more source training documents.

Think of yourself as the subject-matter expert standing in front of a classroom. Before
writing a single section title, ask yourself:

  "If I had only 60 minutes with these students, which topics would I absolutely have
   to cover for them to walk away confident and competent — and which could I cut
   without hurting them?"

That standard should govern every decision below.

═══════════════════════════════════════════════════════════
TRAINER'S MINDSET — READ THIS FIRST
═══════════════════════════════════════════════════════════
ONLY include a topic if it passes at least one of these tests:

  ✔  A student WILL encounter this on the job or in a real exam scenario.
  ✔  Misunderstanding this concept causes real-world mistakes or compliance failures.
  ✔  This is a prerequisite that unlocks understanding of a later critical topic.

EXCLUDE a topic if it is:

  ✗  Background trivia that professionals already know or can look up in 30 seconds.
  ✗  A near-duplicate of another section (same concept, different wording).
  ✗  Institutional/regulatory history that has no bearing on current practice.
  ✗  An administrative or procedural detail that belongs in a reference manual, not a course.

QUALITY STANDARD FOR SUBTOPICS:
  Each subtopic must represent a discrete, teachable idea a student can act on.
  "Overview" and "Introduction" are not subtopics — they are transitions.
  A list of five near-identical subtopics is a sign that a section needs to be
  consolidated, not expanded.

CONTENT OBJECTIVES ("content" field):
  Write each section's content objective the way a trainer introduces a lesson:
  "In this section, students will learn to [do / identify / apply / explain] …"
  Make it practical and specific — NOT "this section covers X and Y".

LEARNING OBJECTIVES:
  Extract objectives from the source. Write or refine them in measurable,
  action-verb form (Bloom's Taxonomy: identify, explain, apply, analyze, distinguish).
  Remove vague objectives like "understand the importance of X" — replace with
  something a student can actually demonstrate.

{_SOURCE_CONTENT_FORMAT_BLOCK}

═══════════════════════════════════════════════════════════
CONTENT SELECTION RULES
═══════════════════════════════════════════════════════════
- Ground every topic in real information from the source — do NOT hallucinate
- Fewer, richer sections beat many thin ones. Aim for depth over breadth.
- FORMAT A (TOC present): use the TOC as the starting skeleton; apply trainer judgment
  to merge, drop, or reorder entries; fill content from the mapped body paragraphs
- FORMAT B (no TOC): derive structure from the headings first, then paragraph content;
  theme-group related paragraphs when no headings exist
- Multiple source documents:
    • Use the most detailed / authoritative version when concepts overlap
    • Do NOT duplicate — one concept, one section
    • Combine complementary content (e.g. regulation text + worked examples) into one rich lesson
- Preserve domain-specific terminology exactly as written in the source

═══════════════════════════════════════════════════════════
CURRICULUM QUALITY RULES
═══════════════════════════════════════════════════════════
1. SELECT CRITICALLY — only topics that pass the trainer's test above
2. MERGE — combine headings that teach the same concept
   e.g. "Types of Policies" + "Policy Types Overview" → "1.0 Policy Types"
3. DEDUPLICATE — never two sections on the same concept
4. SEQUENCE FOR LEARNING — foundational definitions first, then mechanics,
   then applied rules, then exceptions and edge cases
5. TITLE PROFESSIONALLY — write lesson titles a professional would use in a
   training catalogue; avoid verbatim raw heading text when it is informal,
   vague, or incomplete
6. SUBTOPIC DISCIPLINE — 3–6 tight, distinct subtopics per section is ideal;
   more than 8 is a signal to split or consolidate the section

═══════════════════════════════════════════════════════════
STRUCTURE & PACING RULES
═══════════════════════════════════════════════════════════
- Each lesson covers one coherent topic (typically 10–25 minutes of instruction)
- Subtopics flow logically within the lesson: context → concept → application
- Leave "interactive_elements" as [] — Knowledge Check placement is handled
  by the KC Planner using rule packs; do not set it here
- minutes     = round(word_count / 180, 1)   (180 words ≈ 1 minute of reading)
- credit_hour = round(minutes / 50, 3)        (50 min = 1.0 credit hour)
- Totals      = sum of all section values

WORD COUNT TARGETS BY DIFFICULTY:
- basic:        400–800 words per section
- intermediate: 800–1500 words per section
- advanced:     1500–2500 words per section (more subtopics, regulatory depth, examples)

PROGRESSION ORDER:
  Definitions / context → Core concepts and rules → Applied scenarios →
  Compliance edge cases / exceptions → (no summary section — see reserved rule below)

RESERVED SECTIONS — NEVER CREATE AS LESSONS:
- "Overview", "Introduction", "Learning Objectives", "Learning Outcomes",
  "Summary", and "Assessment" are structural placeholders, NOT content lessons.
  • "description" captures the course overview.
  • "learning_objectives" captures the objectives.
  • Treat their body text as metadata; do not turn them into sections.
- NEVER nest course topics as subtopics under "Learning Objectives" or "Overview".
  Every content topic must appear as an independent top-level section.

═══════════════════════════════════════════════════════════
OUTPUT SCHEMA
═══════════════════════════════════════════════════════════
Return ONLY a single JSON object — no markdown, no explanation:

{json.dumps(_GENERATE_TO_format, indent=2)}

FIELD RULES:
- "course_title": derive from document title or primary topic
- "course_id": course ID from document if present, else ""
- "description": 2–4 sentence professional summary written for a student:
    who this course is for, what they will be able to do after completing it,
    and why it matters in their professional context
- "learning_objectives": measurable, action-verb outcome statements (Bloom's verbs)
    extracted or refined from source material; remove vague intent statements
- "sections": ordered lesson list — only sections that survive the trainer's test
  - "title": "N.0 Topic Name" (e.g. "1.0 Flood Insurance Fundamentals")
  - "content": trainer-style objective — "Students will learn to [action] …"
               1–2 sentences; specific and practical, not a table-of-contents summary
  - "subtopics": 3–6 distinct, actionable subtopic title strings per section;
                 curriculum-style (not raw heading text)
  - "word_count": string (e.g. "1250")
  - "minutes": string derived from word_count (e.g. "6.9")
  - "credit_hour": string derived from minutes (e.g. ".14")
  - "interactive_elements": [] always
  - "para_idx_start": integer from [P<N>] prefix for FIRST paragraph of this section
  - "para_idx_end":   integer from [P<N>] prefix for LAST paragraph (inclusive)
    → Set null when no [P<N>] indices are present
- "totals": {{"word_count": "<sum>", "minutes": "<sum>", "credit_hours": "<sum>"}}

PARA INDEX RULES:
- Sections must be contiguous and non-overlapping within each source document
- First section's para_idx_start = first meaningful content paragraph (skip title/headers)
- Last section's para_idx_end = last content paragraph in that document's block
- When multiple files are present, indices are scoped per ``--- Document: ---`` block

Output ONLY valid JSON. No explanation. No markdown fences.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic TO generation helpers
# ─────────────────────────────────────────────────────────────────────────────

#: Default course duration used when the FE does not supply one (legacy paths).
DEFAULT_TO_DURATION_HOURS: int = 3

#: NAIC CE difficulty multipliers (same values as outline_metrics.py).
_DIFFICULTY_MULTIPLIERS: dict[str, float] = {
    "basic":        1.00,
    "intermediate": 1.25,
    "advanced":     1.50,
}


def compute_calculated_word_count(duration_hours: int | float, difficulty: str) -> int:
    """Return target word count from duration + difficulty.

    Formula: (duration_hours × 9,000) / multiplier
    Mirrors the identical calculation performed by the frontend.
    """
    mult = _DIFFICULTY_MULTIPLIERS.get((difficulty or "intermediate").lower(), 1.25)
    return max(1, round((duration_hours * 9000) / mult))


def build_dynamic_to_prompt(
    duration_hours: int | float,
    difficulty_level: str,
    calculated_word_count: int,
    audience: str | None = None,
) -> str:
    """Build the dynamic system prompt for LLM-based TO generation.

    Used when the user selects a course duration, difficulty level, and (optionally)
    target audience from the UI.  The LLM receives the structured document content
    (heading_tree or TOC hierarchy) in the user message alongside this prompt.

    Args:
        duration_hours:        Course duration selected by the user (e.g. 3).
        difficulty_level:      "basic" | "intermediate" | "advanced".
        calculated_word_count: Target total words = (duration_hours × 9000) / multiplier.
        audience:              Target audience string from the audience popup (e.g.
                               "trained insurance agents"). When provided, the prompt
                               instructs the LLM to tailor topic selection, examples,
                               vocabulary, and learning objectives for this audience.
    """
    _difficulty_cap = difficulty_level.strip().capitalize()
    _difficulty_low = difficulty_level.strip().lower()

    # Per-difficulty behavioral guidance injected into the prompt so the LLM
    # knows how to adjust depth, breadth, vocabulary, and example density.
    _DIFFICULTY_GUIDANCE: dict[str, str] = {
        "basic": (
            "BASIC level — treat the student as a complete beginner:\n"
            "  • SELECT foundational concepts only — what every practitioner must know from day one.\n"
            "  • AVOID advanced regulatory nuance, edge cases, and complex cross-references.\n"
            "  • USE plain language; define every industry term the first time it appears.\n"
            "  • INCLUDE more worked examples and scenario-based subtopics than you would at higher levels.\n"
            "  • KEEP sections focused on a single core concept per lesson — no dense multi-concept sections.\n"
            "  • OMIT topics that require prior regulatory or product knowledge to understand."
        ),
        "intermediate": (
            "INTERMEDIATE level — student has foundational knowledge; deepen without overwhelming:\n"
            "  • SELECT topics that build on core principles — mechanics, application, and compliance practice.\n"
            "  • INCLUDE some regulatory nuance and practical scenarios, but anchor each in a concrete example.\n"
            "  • BALANCE breadth and depth — cover more ground than Basic but go deeper on the most critical topics.\n"
            "  • ASSUME familiarity with key terms; define only specialized or context-specific terminology.\n"
            "  • SEQUENCE from concept → applied rule → real-world implication within each section."
        ),
        "advanced": (
            "ADVANCED level — student is an experienced professional; provide analytical depth:\n"
            "  • SELECT topics that cover regulatory edge cases, exceptions, compliance judgment calls, and\n"
            "    complex cross-product or cross-regulatory interactions.\n"
            "  • GO DEEP on nuance — explain WHY rules exist and how they interact, not just WHAT they say.\n"
            "  • INCLUDE analysis-level subtopics: suitability determinations, disclosure obligations under\n"
            "    specific fact patterns, regulatory gray areas.\n"
            "  • USE professional terminology without simplification; the student can handle it.\n"
            "  • PRIORITIZE application, analysis, and evaluation over definitional content.\n"
            "  • INCLUDE subtopics on common compliance failures and how to avoid them."
        ),
    }
    _diff_guidance = _DIFFICULTY_GUIDANCE.get(
        _difficulty_low,
        _DIFFICULTY_GUIDANCE["intermediate"],
    )

    # Duration-based section count guidance — prevents the LLM from generating
    # 15 sections for a 1-hour course or 4 sections for a 5-hour course.
    _SECTION_BUDGET: dict[int, tuple[int, int]] = {
        1: (3, 5),
        2: (5, 8),
        3: (8, 12),
        4: (10, 15),
        5: (12, 18),
    }
    _dur_int = max(1, min(5, int(round(float(duration_hours)))))
    _sec_min, _sec_max = _SECTION_BUDGET.get(_dur_int, (8, 12))

    # Audience block — only injected when an audience is specified.
    _audience_block = ""
    if audience and audience.strip():
        _audience_block = f"""\
═══════════════════════════════════════════════════════════
TARGET AUDIENCE
═══════════════════════════════════════════════════════════
Audience: {audience.strip()}

Tailor EVERY decision below for this specific audience:
  • TOPIC SELECTION  — Include topics this audience encounters on the job or in
    their regulatory environment. Exclude topics that are irrelevant to their role.
  • VOCABULARY       — Use the terminology this audience is trained in. Avoid
    dumbing down if they are professionals; avoid jargon if they are newcomers.
  • EXAMPLES         — Ground every scenario in a situation this audience faces
    (e.g. for insurance agents: client suitability conversations, policy endorsements,
    state DOI filings; for broker-dealer reps: FINRA exam prep, AML procedures).
  • LEARNING OBJECTIVES — Write objectives as outcomes this audience will apply
    in their specific professional context, not generic knowledge statements.
  • PREREQUISITES    — Assume the knowledge and experience level appropriate
    for this audience; neither over-explain nor under-explain.

"""

    _section_schema = {
        "title": "1. Introduction",
        "content": "Students will learn to ...",
        "subtopics": [],
        "word_count": 2200,
        "minutes": 12.22,
        "credit_hour": 0.244,
        "interactive_elements": [],
        "para_idx_start": None,
        "para_idx_end": None,
    }
    _subtopic_schema = {
        "title": "2.1 Community",
        "content": "",
        "word_count": 72,
        "minutes": 0.4,
        "credit_hour": 0.008,
        "interactive_elements": [],
    }
    _main_schema = {
        "course_title": "Course name",
        "course_id": "",
        "description": "2-4 sentences: who this course is for, what they will be able to do, why it matters",
        "learning_objectives": ["Explain ...", "Identify ..."],
        "sections": [_section_schema],
        "totals": {
            "word_count": calculated_word_count,
            "minutes": round(calculated_word_count / 180, 2),
            "credit_hours": round((calculated_word_count / 180) / 50, 3),
        },
    }

    import json as _json
    return f"""\
IMPORTANT: Your response MUST be a single valid JSON object ONLY.
Do NOT output markdown, headings, prose, or any text outside the JSON object.
Start your response with "{{" and end with "}}". No code fences. No explanation.

You are a seasoned industry trainer and curriculum designer. The user message contains
course content extracted from source documents. Your task: design a Timed Outline (TO)
that an instructor could teach in exactly the time and at the depth specified below.

{_audience_block}\
═══════════════════════════════════════════════════════════
COURSE CONFIGURATION
═══════════════════════════════════════════════════════════
Course Duration:   {duration_hours} hour{'s' if duration_hours != 1 else ''}
Difficulty Level:  {_difficulty_cap}
Target Word Count: {calculated_word_count:,} words
Section Budget:    {_sec_min}–{_sec_max} top-level sections for a {duration_hours}-hour course

═══════════════════════════════════════════════════════════
DIFFICULTY REQUIREMENTS — {_difficulty_cap.upper()}
═══════════════════════════════════════════════════════════
{_diff_guidance}

═══════════════════════════════════════════════════════════
DURATION & TOPIC SELECTION RULES
═══════════════════════════════════════════════════════════
This is a {duration_hours}-hour course. Every topic selection decision must be made
with this constraint in mind:

  1. TARGET {_sec_min}–{_sec_max} SECTIONS. A {duration_hours}-hour course cannot
     adequately cover more than {_sec_max} topics. If the source has more candidates,
     select only the most critical ones and merge or drop the rest.

  2. PRIORITIZE ruthlessly:
       ✔ Topics that directly affect job performance or compliance obligations
       ✔ Topics that students will encounter within the first 90 days on the job
       ✔ Topics explicitly required by the regulatory CE standard for this family
       ✗ Background history that does not affect current practice
       ✗ Topics covered in prerequisites or other courses in the series
       ✗ Administrative details that belong in a reference manual

  3. BALANCE: allocate more word count to complex, high-stakes topics and less
     to foundational or transitional ones. A section's word count should reflect
     its instructional weight, not just its presence in the source document.

  4. COMPLETE COVERAGE: ensure all major subject areas in the source are
     represented — do not omit an entire chapter or regulatory domain just
     because other topics are more interesting. If a topic is too thin for its
     own section, merge it as a subtopic of the nearest related section.

═══════════════════════════════════════════════════════════
TRAINER'S MINDSET — READ BEFORE SELECTING TOPICS
═══════════════════════════════════════════════════════════
Think of yourself as a trainer standing in front of this exact audience for exactly
{duration_hours} hour{'s' if duration_hours != 1 else ''}. Before writing any section title, ask:

  "If I had only this time with these students, which topics would they absolutely
   need to walk away confident and competent — and which could I cut without
   hurting them professionally?"

ONLY include a topic if it passes at least one of:
  ✔  Students WILL encounter this on the job or in a real compliance scenario.
  ✔  Misunderstanding this concept causes real-world mistakes or regulatory failures.
  ✔  This is a prerequisite that unlocks understanding of a later critical topic.

EXCLUDE a topic if it is:
  ✗  Background trivia professionals already know or can look up in 30 seconds.
  ✗  A near-duplicate of another section (same concept, different wording).
  ✗  Regulatory history with no bearing on current practice.
  ✗  An administrative detail that belongs in a reference manual, not a course.

═══════════════════════════════════════════════════════════
WORD COUNT & CREDIT FORMULA
═══════════════════════════════════════════════════════════
180 words  = 1 reading minute
50 minutes = 1 CE credit hour
9,000 words = 1 base CE hour

Difficulty multipliers (NAIC CE):
  Basic        1.00× → {int(1 * 9000)} base words/hr
  Intermediate 1.25× → {int(1.25 * 9000)} base words/hr
  Advanced     1.50× → {int(1.5 * 9000)} base words/hr

This course: {duration_hours} × 9,000 / {_DIFFICULTY_MULTIPLIERS[_difficulty_low]}× = {calculated_word_count:,} words

Distribute {calculated_word_count:,} words proportionally across sections weighted
by topic depth and importance. Each section's word count drives its minutes and
credit_hour values.

{_SOURCE_CONTENT_FORMAT_BLOCK}

═══════════════════════════════════════════════════════════
STRUCTURAL & OUTPUT RULES
═══════════════════════════════════════════════════════════
CURRICULUM QUALITY:
  1. SELECT CRITICALLY — trainer's test above; cut low-value topics
  2. MERGE — combine headings teaching the same concept into one section
  3. DEDUPLICATE — never two sections on the same concept
  4. SEQUENCE — foundational definitions → core mechanics → application → compliance edge cases
  5. TITLE PROFESSIONALLY — titles a trainer would use in a real training catalogue

SECTION PACING:
  - Each section covers one coherent topic (typically 10–25 minutes of instruction)
  - 3–6 subtopics per section is ideal; more than 8 signals over-splitting
  - Subtopics flow: context → concept → application

SOURCE FIDELITY — CRITICAL:
  Any title or heading from the source MUST remain EXACTLY as written.
  Do NOT rename, paraphrase, beautify, merge, or modify wording.
  STRIP trailing "page N" / "pg N" / "p. N" from ALL titles.
  Examples:
    "1.0 Anywhere There Is Water page 1"  →  "1.0 Anywhere There Is Water"
    "5.6 Cancellations pg 22"             →  "5.6 Cancellations"

RESERVED SECTIONS — NEVER create as content lessons:
  "Overview", "Introduction", "Learning Objectives", "Learning Outcomes",
  "Course Objectives", "Summary", "Assessment"
  → Capture in "description" / "learning_objectives" fields instead.

KNOWLEDGE CHECKS:
  - NEVER add "Knowledge Check" as a subtopic entry.
  - If a KC appears in the source, add "knowledge_check" to the PARENT section's
    "interactive_elements" list. All other "interactive_elements" stay [].

TIMING FORMULAS:
  minutes     = round(word_count / 180, 2)
  credit_hour = round(minutes / 50, 3)
  Totals      = sum of all section values (target ≈ {calculated_word_count:,} words)

═══════════════════════════════════════════════════════════
OUTPUT FORMAT — return ONLY valid JSON, no markdown fences
═══════════════════════════════════════════════════════════
{_json.dumps(_main_schema, indent=2)}

Section schema:
{_json.dumps(_section_schema, indent=2)}

Subtopic schema (use when subtopic has its own timing data):
{_json.dumps(_subtopic_schema, indent=2)}

SUBTOPICS — OBJECTS vs PLAIN STRINGS:
  - Subtopic with own word count / timing → emit as object (subtopic schema above).
  - Subtopic with no timing data → plain title string is acceptable.
  - NEVER include "Knowledge Check" in subtopics.

FIELD RULES:
- "course_title": derive from primary source document title
- "course_id": course ID from source if present, else ""
- "description": 2–4 sentence professional summary (audience, outcomes, importance)
- "learning_objectives": measurable action-verb statements (Bloom's Taxonomy verbs)
  tailored to the target audience and difficulty level
- "sections": ordered lesson list — only sections that pass the trainer's test
  - "title": EXACTLY as in source (page refs stripped)
  - "content": trainer-style objective — "Students will learn to [action] …"
  - "subtopics": objects (preferred) or plain strings; NEVER includes KC entries
  - "word_count": integer — proportional share of {calculated_word_count:,} total
  - "minutes": float — word_count / 180
  - "credit_hour": float — minutes / 50
  - "interactive_elements": [] unless KC found in source (then ["knowledge_check"])
  - "para_idx_start": integer index of first paragraph (from [P<N>] tags); null if absent
  - "para_idx_end": integer index of last paragraph (inclusive); null if absent
- "totals": sums across all sections (target total ≈ {calculated_word_count:,} words)

Return ONLY valid JSON.  No explanation.  No markdown fences.
"""


CLASSIFICATIONTO_OUTLINE_PROMPT = f"""\
You are an expert curriculum parser. Extract structured data from a Timed Outline (TO) document.

The document contains single-cell tables followed by a 7-column outline grid:

  • First single-cell table  → course_title  (value after the "COURSE TITLE:" label)
  • Second single-cell table → course_id     (value after the "COURSE ID:" label)
  • Third single-cell table  → description   (course description prose)
  • Fourth single-cell table → learning_objectives (one objective per line)
  • 7-column outline table   → sections (skip the header row; last row = totals)
      Column 0  Lesson Topic         → "title"
      Column 1  Subtopic             → subtopic names (split on newline; include each subtopic/knowledge-check line as a separate item)
      Column 2  Content Objective    → "content" (copy as-is; use "" when blank)
      Column 3  Word Count           → "word_count"
      Column 4  Minutes              → "minutes"
      Column 5  Credit Hour          → "credit_hour"
      Column 6  Interactive Elements → "interactive_elements" (split on comma into a list)

Return ONLY a single JSON object matching this exact schema — no markdown, no explanation:

{json.dumps(TO_outline_format, indent=2)}

Parsing rules:
- Do NOT hallucinate — only use text present in the document
- Strip leading/trailing whitespace from all values
- If a field is blank or absent → use "" for strings, [] for arrays
- "subtopics": split the Subtopic column (Col 1) on newline; keep numbered entries (e.g. "1.1 Coverage") as separate list items — NEVER include "Knowledge Check" entries as subtopics (see rule below)

TITLE NORMALISATION — CRITICAL:
- STRIP any trailing "page N" / "pg N" / "p. N" reference from EVERY title
  (both top-level section titles and subtopic titles).
- Examples:
    "1.0 Anywhere There Is Water page 1"   →  "1.0 Anywhere There Is Water"
    "2.3 Ineligible Property page 3"       →  "2.3 Ineligible Property"
    "5.6 Cancellations pg 22"              →  "5.6 Cancellations"
- These page numbers are layout references for the source PDF and must NEVER appear in extracted titles.
- "interactive_elements": split on comma; trim each item; omit "n/a" / "N/A" entries
- "word_count", "minutes", "credit_hour": copy the raw string as written (e.g. "4115", "23", ".46")
- "totals": read from the last row of the outline table (the row whose Lesson Topic cell is blank or says "Totals")
- Output ONLY valid JSON

RESERVED SECTION RULE — CRITICAL:
If a section's Col 0 title (ignoring a leading "N.0 " number prefix) is one of:
  "Overview", "Introduction", "Learning Objectives", "Learning Outcomes",
  "Course Objectives", "Summary", "Assessment"
  → Add the section to "sections" as-is (it may legitimately appear in the TO).
  → Its "subtopics" list MUST be [] (empty) — NEVER put course topic/module names
    inside a Learning Objectives or Overview section's subtopics.
  → Objective text lines listed under a Learning Objectives row are metadata,
    not subtopics; discard them from the subtopics list.

KNOWLEDGE CHECK RULE — CRITICAL:
If a row or a subtopic item has "Knowledge Check" anywhere in its title:
  → NEVER add it as a subtopic (not as a string, not as an object).
  → Instead, add "knowledge_check" to the PARENT section's "interactive_elements" list.
  → Discard the timing data for that row (it is accounted for in the parent section total).

SUBTOPICS AS OBJECTS — breakdown documents:
Some documents have a separate row for each subtopic (e.g. "2.1 Community", "2.2 Eligible Buildings")
with its own word_count / minutes / credit_hour columns.

IF a row's Col 0 matches pattern N.M or N.M.P (e.g. "2.1", "3.2", "2.1.1") AND
at least one of word_count / minutes / credit_hour for that row is non-blank AND
the title does NOT contain "Knowledge Check":
  → Do NOT add it as a top-level section.
  → Instead, add it as an OBJECT inside the nearest parent section's "subtopics" list:
    {{
      "title":               "<subtopic title from Col 0>",
      "content":             "<Col 2 or ''>",
      "word_count":          "<Col 3 or ''>",
      "minutes":             "<Col 4 or ''>",
      "credit_hour":         "<Col 5 or ''>",
      "interactive_elements": [<Col 6 split on comma, omit n/a>]
    }}

IF a subtopic row (N.M) has NO timing data at all → add its title as a plain string
to the parent's "subtopics" list (original behaviour).

Example — breakdown document:
  Row: Col0="2.0 NFIP Background"      Col3="198"  Col4="1.1"   Col5=".022"
  Row: Col0="2.1 Community"            Col3="72"   Col4="0.4"   Col5=".008"
  Row: Col0="2.2 Eligible Buildings"   Col3="90"   Col4="0.5"   Col5=".01"
  Row: Col0="Knowledge Check page 5"   Col3="180"  Col4="1.0"   Col5=".02"  ← KC row → skip as subtopic

  Output for the 2.0 section  (KC row → "knowledge_check" in interactive_elements, NOT in subtopics):
  {{
    "title": "2.0 NFIP Background", "word_count": "198", "minutes": "1.1", "credit_hour": ".022",
    "interactive_elements": ["knowledge_check"],
    "subtopics": [
      {{"title": "2.1 Community",          "word_count": "72",  "minutes": "0.4", "credit_hour": ".008", "content": "", "interactive_elements": []}},
      {{"title": "2.2 Eligible Buildings", "word_count": "90",  "minutes": "0.5", "credit_hour": ".01",  "content": "", "interactive_elements": []}}
    ]
  }}

Example — flat document (subtopics only in Col 1, no separate rows):
  Row: Col0="2.0 NFIP Game"  Col3="2765"  Col4="15.4"  Col5=".31"
       Col1="2.1 Urban Areas\\n2.1.1 Case Study\\nKnowledge Check\\n2.2 Renters"

  Output for the 2.0 section  (KC in Col1 → "knowledge_check" in interactive_elements, NOT in subtopics):
  {{
    "title": "2.0 NFIP Game", "word_count": "2765", "minutes": "15.4", "credit_hour": ".31",
    "interactive_elements": ["knowledge_check"],
    "subtopics": ["2.1 Urban Areas", "2.1.1 Case Study", "2.2 Renters"]
  }}
"""
