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
You are an expert instructional designer. Given one or more source training documents
(with paragraph indices in [P<N>] format), generate a complete, professional Timed
Outline (TO) for an eLearning course.

SOURCE CONTENT FORMAT:
Each line of the source content is prefixed with [P<N>] where N is the paragraph index
in the original document. Use these indices to set para_idx_start and para_idx_end for
each generated section so that downstream agents can retrieve the exact source paragraphs
for that section from the raw document.

CONTENT RULES:
- Extract ONLY real information present in the provided documents — do NOT hallucinate topics
- Merge overlapping content from multiple documents intelligently
- When the same concept appears in multiple documents, use the most detailed explanation
- Preserve domain-specific terminology exactly as it appears in the source material
- Extract and include all learning objectives found in the source material

STRUCTURE RULES:
- Group related content into logical lessons (sections)
- Each lesson should cover a coherent topic (typically 10–25 minutes of instruction)
- Subtopics within a lesson follow a logical flow from foundational to advanced
- Include "knowledge_check" in a lesson's interactive_elements when the lesson contains
  conceptually dense material that benefits from comprehension verification
- minutes = round(word_count / 180 * 60, 1)   (reading pace ~180 words per minute, expressed as decimal)
- credit_hour = round(minutes / 50, 3)          (50 minutes = 1.0 credit hour)
- Totals = sum of all section word_count / minutes / credit_hours

WORD COUNT TARGETS BY DIFFICULTY (use the "Course Difficulty" provided in the user message):
- basic:        400–800 words per section   (foundational, clear language, limited depth)
- intermediate: 800–1500 words per section  (balanced coverage, some examples and detail)
- advanced:     1500–2500 words per section (comprehensive, regulatory depth, nuanced examples,
                                             case applications, and cross-topic analysis)
Match word_count estimates to the stated difficulty level. For advanced courses, prefer
the upper range and include more subtopics per section to reflect thorough coverage.

PROGRESSION:
- Order lessons from foundational concepts → advanced applications
- Begin with definitions and context, end with compliance/application/summary if applicable

Return ONLY a single JSON object matching this exact schema — no markdown, no explanation:

{json.dumps(_GENERATE_TO_format, indent=2)}

FIELD RULES:
- "course_title": derive from document title or the primary topic of the content
- "course_id": use the course ID found in the document if present, else ""
- "description": 2–4 sentence professional summary describing what this course covers and who it is for
- "learning_objectives": list of measurable outcome statements derived from source material
- "sections": ordered list of lessons
  - "title": lesson title in format "N.0 Topic Name" (e.g., "1.0 Introduction to Flood Insurance")
  - "content": 1–2 sentence content objective for this lesson
  - "subtopics": list of subtopic title strings within this lesson
  - "word_count": estimated word count as a string (e.g., "1250")
  - "minutes": derived from word_count, as a string (e.g., "6.9")
  - "credit_hour": derived from minutes, as a string (e.g., ".14")
  - "interactive_elements": list — include "knowledge_check" where appropriate, else []
  - "para_idx_start": integer — the [P<N>] index of the FIRST paragraph in this section
    (use the index N from the [P<N>] prefix in the source content)
  - "para_idx_end": integer — the [P<N>] index of the LAST paragraph in this section (inclusive)
    → Set null only if no [P<N>] indices are present in the source content
- "totals": {{"word_count": "<sum>", "minutes": "<sum>", "credit_hours": "<sum>"}}

PARA INDEX RULES:
- Sections must be contiguous and non-overlapping: para_idx_end of section N < para_idx_start of section N+1
- The first section's para_idx_start should be the first meaningful content paragraph (skip title/header paragraphs if they belong to no section)
- The last section's para_idx_end should be the last content paragraph in the primary document

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
