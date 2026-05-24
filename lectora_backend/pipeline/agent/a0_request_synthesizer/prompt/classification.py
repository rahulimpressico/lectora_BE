"""
LLM classification prompt for A0 — Request Synthesizer.
"""

import json

from lectora_backend.pipeline.rule_pack_config.timed_outline import TO_outline_format

CLASSIFICATION_PROMPT = """\
You are a course classification engine. Given a course title, learning objectives,
and a content sample, classify the course into exactly ONE of these rule families:

  - insurance_ce     -> Insurance Continuing Education (P&C, life, health, flood, etc.)
  - iarce            -> Investment Adviser / Registered Rep CE (securities, FINRA, annuities)
  - firm_element     -> Firm Element compliance training (broker-dealer supervisory, annual compliance)

Respond with ONLY a JSON object — no markdown, no explanation:
{
  "rule_family": "<one of: insurance_ce | iarce | firm_element>",
  "confidence": <float 0-1>,
  "audience": "<who this course is for>",
  "course_type": "<e.g. Self-study CE, Classroom CE, Webinar, etc.>",
  "category": "<specific sub-category, e.g. Property & Casualty — Flood Insurance>",
  "topic": "<primary topic>",
  "reasoning": "<1-2 sentence justification>"
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

GENERATE_TO_PROMPT = f"""\
You are an expert instructional designer creating a professional Timed Outline (TO)
for an eLearning course from one or more source training documents.

═══════════════════════════════════════════════════════════
SOURCE CONTENT FORMAT
═══════════════════════════════════════════════════════════
The user message may contain:

1. SOURCE DOCUMENT CONTENT (with paragraph indices)
   Multiple files are separated by ``--- Document: <filename> ---`` headers.
   Within each block, lines are prefixed with [P<N>] where N is the paragraph
   index in that file. Use indices from the matching document block for
   para_idx_start and para_idx_end on each section.

2. DOCUMENT HEADING STRUCTURE (optional)
   A structured outline of headings detected across all uploaded files.
   Format: [L<level>] <heading_text>  (L1 = top-level, L2 = sub-topic, etc.)
   When present, USE this as the primary structural skeleton for the TO.
   Translate headings → lessons intelligently — do NOT copy headings blindly:
     • Merge closely related headings into one lesson
     • Elevate sub-headings to top-level lessons if they represent major topics
     • Remove duplicate or trivially similar headings
     • Re-order for logical curriculum flow (foundations first)

3. COURSE TYPE CONTEXT (optional)
   A hint about the regulatory domain (e.g. "Washington LTC Compliance").
   When present:
     • Prioritize topics directly relevant to that domain
     • De-prioritize off-topic sections
     • Use domain-appropriate terminology
     • Filter unrelated extracted topics from the outline

═══════════════════════════════════════════════════════════
CONTENT EXTRACTION RULES
═══════════════════════════════════════════════════════════
- Extract ONLY real information present in the source documents — do NOT hallucinate topics
- When heading structure is provided: derive the TO structure from headings first,
  then fill content objectives and subtopics from the indexed paragraph content
- When NO heading structure is provided: use AI to identify important topics and
  concepts from the paragraph content, group by theme, and generate logical lessons
- Merge overlapping content from multiple documents:
    • When the same concept appears across files, use the most detailed version
    • Do NOT create duplicate lessons for the same regulatory concept
    • Combine complementary material (e.g. law text + examples) into one lesson
- Preserve domain-specific terminology exactly as it appears in the source

═══════════════════════════════════════════════════════════
TOPIC QUALITY RULES (curriculum-style, not raw extraction)
═══════════════════════════════════════════════════════════
1. MERGE: Combine related headings under one lesson
   e.g. "Types of Policies" + "Policy Types Overview" → "1.0 Policy Types"
2. DEDUPLICATE: Never create two sections covering the same concept
3. ORGANIZE LOGICALLY: foundational → applied → compliance/assessment
4. RENAME for curriculum clarity: prefer professional lesson titles over
   verbatim heading text when the original is informal or incomplete
5. FILTER by course type when a context hint is provided

═══════════════════════════════════════════════════════════
STRUCTURE & PACING RULES
═══════════════════════════════════════════════════════════
- Each lesson covers a coherent topic (typically 10–25 minutes of instruction)
- Subtopics follow a logical flow from foundational to advanced within the lesson
- Leave "interactive_elements" as [] — Knowledge Check placement is handled by the KC Planner using rule packs
- minutes = round(word_count / 180 * 60, 1)   (~180 wpm reading pace)
- credit_hour = round(minutes / 50, 3)          (50 min = 1.0 credit hour)
- Totals = sum of all section values

WORD COUNT TARGETS BY DIFFICULTY:
- basic:        400–800 words per section
- intermediate: 800–1500 words per section
- advanced:     1500–2500 words per section (include more subtopics, regulatory depth)

PROGRESSION ORDER:
  Definitions/context → Core concepts → Applied rules → Compliance/exceptions → Summary

RESERVED SECTIONS — NEVER CREATE AS LESSONS:
- "Overview", "Introduction", "Learning Objectives", "Learning Outcomes", and
  "Summary" / "Assessment" are NOT content lessons — do NOT add them to "sections".
  • The "description" field already captures the course overview.
  • The "learning_objectives" field already captures the objectives.
  • If the source document has these as headings, treat their body text as metadata,
    not as lesson content to replicate.
- NEVER nest course topics or modules as subtopics under "Learning Objectives"
  or "Overview". Course topics must ALWAYS appear as independent top-level lessons.

═══════════════════════════════════════════════════════════
OUTPUT SCHEMA
═══════════════════════════════════════════════════════════
Return ONLY a single JSON object — no markdown, no explanation:

{json.dumps(_GENERATE_TO_format, indent=2)}

FIELD RULES:
- "course_title": derive from document title or primary topic
- "course_id": course ID from document if present, else ""
- "description": 2–4 sentence professional summary (who it is for + what it covers)
- "learning_objectives": measurable outcome statements from source material
- "sections": ordered lesson list
  - "title": "N.0 Topic Name" (e.g. "1.0 Introduction to Flood Insurance")
  - "content": 1–2 sentence content objective for this lesson
  - "subtopics": list of subtopic title strings (curriculum-style, not raw heading text)
  - "word_count": string (e.g. "1250")
  - "minutes": string derived from word_count (e.g. "6.9")
  - "credit_hour": string derived from minutes (e.g. ".14")
  - "interactive_elements": [] always — Knowledge Check placement is determined by the KC Planner, not TO generation
  - "para_idx_start": integer from [P<N>] prefix for FIRST paragraph of this section
  - "para_idx_end":   integer from [P<N>] prefix for LAST paragraph (inclusive)
    → Set null when no [P<N>] indices are present (PDF-only sources)
- "totals": {{"word_count": "<sum>", "minutes": "<sum>", "credit_hours": "<sum>"}}

PARA INDEX RULES:
- Sections must be contiguous and non-overlapping within each source document
- First section's para_idx_start = first meaningful content paragraph (skip title/headers)
- Last section's para_idx_end = last content paragraph in that document's block
- When multiple files are present, indices are scoped per ``--- Document: ---`` block

Output ONLY valid JSON. No explanation. No markdown fences.
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
