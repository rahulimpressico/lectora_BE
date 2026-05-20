# Lactora - Multi-Agent Course Authoring Pipeline

A multi-agent pipeline that transforms raw `.docx` study guides into structured, compliance-validated, and styled course content for RegEd Inc.

Built for Insurance CE, IARCE, and Firm Element continuing education courses.

---

## Architecture

**Orchestrator:** [`pipeline.py`](pipeline.py) runs `run_pipeline(docx_path, to_outline_doc_path)`. If **S1** returns **blocked**, it re-runs the full **A0 → A1 → S1** chain (up to 3 cycles), passing prior S1 **feedback** into A1 on retries. Agent code lives under `agent/<package>/main.py`.

**Shared across every agent:**

| Shared item              | What it is                                                                                                                                                                                                                                   | Used by                                                                          |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| **Per-run shared state** | `{stem}_shared_state.json` in `shared_state/` (`stem` = `course_id` when present, else `run_id`), plus sidecars: `request_spec`, `provenance_log`, `llm_to_outline`, `course_spec`, `s1_validation`, generated content, and `{stem}_images/` | A0 (writes bootstrap + outline LLM artefact), A1/S1/A2 update state and sidecars |
| **Source `.docx` files** | **Study guide** path: same `docx_path` for A0, A1, A2. **Timed outline (TO)** `to_outline_doc_path`: second `.docx` whose text A0 sends to the outline LLM (`classify_to_outline_with_llm`)                                                  | A0 only for TO doc; structure/outline parsing still uses the study guide         |
| **Environment**          | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` from `.env`                                                                                                                                                                                  | Any stage that calls Azure OpenAI                                                |
| **Rule packs**           | Single source: `rule_pack_config/rule_packs.py` (`RULE_PACKS`, `get_rule_pack`). TO outline LLM skeleton: `rule_pack_config/timed_outline.py`.                                                                                               | A0, S1, A2                                                                       |

Agents do not import each other’s classes for control flow—the orchestrator wires them together, and **shared state + document paths** are the common contract. See [Shared State](#shared-state) for the JSON shape.

```
                  +==============================================================+
                  |  pipeline.py — orchestrator                                  |
                  |  run_pipeline(): (A0->A1->S1) x3 if S1 blocked              |
                  |  Stage code: one main.py per package in agent/ (see table)   |
                  |--------------------------------------------------------------|
                  |  Shared with every agent:                                    |
                  |   • shared_state/{run_id}_*.json, sidecars, {run_id}_images/ |
                  |   • docx_path + to_outline_doc_path (A0); study guide for A1/A2   |
                  |   • AZURE_OPENAI_* from .env                                |
                  |   • rules: rule_pack_config/rule_packs.py (A0, S1, A2)        |
                  +==============================================================+
                                                 |
                                                 v
                        +-------------------+
                        |   Input (.docx)   |
                        |   Study Guide     |
                        +--------+----------+
                                 |
                                 v
                  +------------------------------+
                  |  A0 - Request Synthesizer    |
                  |  & Input Normalizer          |
                  |------------------------------|
                  |  - Extract metadata (SG .docx)|
                  |  - TO .docx -> outline LLM   |
                  |  - LLM classify rule family  |
                  |  - Extract images, rule pack   |
                  |  - Sidecar: llm_to_outline.json|
                  |  - Init shared state         |
                  +-------------+----------------+
                                |
                      shared_state.json
                                |
                                 v
                  +------------------------------+
                  |  A1 - Timed Outline          |
                  |  Interpreter (LangGraph)     |
                  |------------------------------|
                  |  - Parse document structure  |
                  |  - Validate learning obj.    |
                  |  - Map images to sections    |
                  |  - Enrich via LLM (Azure OpenAI) |
                  |  - Build course_spec JSON    |
                  |  - Detect inconsistencies    |
                  +-------------+----------------+
                                |
                      course_spec.json
                                |
                                v
                  +------------------------------+
                  |  S1 - Stage 1 Validator      |
                  |  (Quality Gate)              |
                  |------------------------------|
                  |  Validates A0 + A1 outputs   |
                  |  against get_rule_pack(...): |
                  |  - Metadata completeness     |
                  |  - Classification confidence |
                  |  - Section structure          |
                  |  - KC count vs rule min      |
                  |  - LO coverage               |
                  |  - Credit hour cross-check   |
                  |  - Assessment rule pre-flight|
                  +-------------+----------------+
                                |
                     Outcome (see Orchestration):
                     - not blocked -> A2
                     - blocked -> A0 -> A1 -> S1 again (max 3 cycles)
                     - still blocked -> end (no A2)
                                |
                                v
                  +------------------------------+
                  |  Section Mapper              |
                  |------------------------------|
                  |  - L1-chapter grouping       |
                  |    on course_spec            |
                  |  - Map groups to TO outline  |
                  |    lessons (sequential)      |
                  |  - enriched_sections in      |
                  |    shared_state              |
                  +-------------+----------------+
                                |
                     enriched_sections.json
                                |
                                v
                  +------------------------------+
                  |  A2 - Content Generator      |
                  |------------------------------|
                  |  - ONE LLM call per lesson   |
                  |    (all subtopics batched)   |
                  |  - Returns JSON array, one   |
                  |    element per subtopic      |
                  |  - Word count from           |
                  |    enriched_sections.json    |
                  |  - Insert images from A0/A1  |
                  |  - Render styled .docx       |
                  |    (matching reference doc)  |
                  +-------------+----------------+
                                |
                      study_guide.docx
                      generated_content.json
                                |
                                v
                  +------------------------------+
                  |  A3–A5 (future)              |
                  |------------------------------|
                  |  Exam / QA / packaging       |
                  +------------------------------+
```

---

## Orchestration (`pipeline.py`)

Signature: `run_pipeline(docx_path: str, to_outline_doc_path: str)`.

The main entrypoint chains agents in this order:

1. **A0 → A1 → S1** — up to **3 full cycles**. Each cycle runs **A0** (new `run_id`, new `{stem}_*` files, study guide + TO, sidecars including **`{stem}_llm_to_outline.json`**), then **A1** (with S1 **feedback** from the previous cycle when retrying), then **S1**. Exit when S1 is **not** blocked. If **A1** fails, stop. If S1 is **blocked**, record feedback and start the next cycle from **A0**.
2. **Section Mapper** — after S1 passes, maps TO outline lessons to `course_spec` sections via deterministic L1-chapter grouping and writes **`enriched_sections`** into shared state (and `{stem}_enriched_sections.json`).
3. **A2** — **Content Generator** in `agent/a2_content_generator/`; runs after the section mapper on a successful pipeline.

If all **3** cycles still end with S1 **blocked**, do not proceed to A2.

`run_pipeline()` prints progress; read artefacts under `shared_state/`.

---

## Pipeline Flow

### A0 - Request Synthesizer & Input Normalizer

Takes two paths: **`docx_path`** (study guide) and **`to_outline_doc_path`** (timed-outline / blueprint `.docx` used only for outline extraction).

From the **study guide**, extracts the four bootstrap fields:

| Input                 | Source                                                   |
| --------------------- | -------------------------------------------------------- |
| `title`               | First Title/large paragraph in doc                       |
| `course_id`           | Regex match for `Course ID: ####`                        |
| `learning_objectives` | List Paragraph items after "Learning Objectives" heading |
| `content_sample`      | Headings + first paragraph per section (~3000 chars)     |

From the **TO document**, text is extracted for a **second LLM** call (`classify_to_outline_with_llm` in `agent/a0_request_synthesizer/utils/classifier.py`), guided by `CLASSIFICATIONTO_OUTLINE_PROMPT` in `prompt/classification.py` and shape hints in `rule_pack_config/timed_outline.py`.

Then:

1. **Extracts images** from the study guide — binaries under `{stem}_images/`, metadata in shared state.
2. **LLM classification** (Azure OpenAI) — rule family (`insurance_ce`, `iarce`, `firm_element`, …) plus inferred metadata.
3. **Outline LLM** — JSON result merged into shared state and **`{stem}_llm_to_outline.json`**.
4. **Loads rule pack** — `rule_pack_config/rule_packs.py` (`RULE_PACKS` / `get_rule_pack`).
5. **Resolves values with provenance** — `explicitly_provided`, `derived_from_rule_pack`, or `inferred`.
6. **Persists** — `request_spec`, `provenance_log`, `shared_state`, **`llm_to_outline`** (paths returned on `A0Result.output_files`).

### A1 - Timed Outline Interpreter

Uses **LangGraph StateGraph** with retry logic and conditional edges:

```
load_shared_state -> parse_document -> validate_los -> map_images
    -> enrich_with_llm -> build_course_spec -> detect_inconsistencies -> persist_output
```

Key principles:

- **Parser owns structure** - sections, word counts, KCs, paragraph ranges are all from python-docx parsing. LLM never touches structural data.
- **LLM enriches only** - Azure OpenAI adds subtopics + LO mapping per section.
- **Image mapping is pure code** - maps images to sections by `para_start <= img.para_idx <= para_end`. No LLM.
- **Retry once** - `parse_document` retries on failure, then enters `failed_end`.
- **Critical stop** - if zero learning objectives, pipeline halts at `stopped_end`.

Output: `course_spec.json` with full section hierarchy, word counts, durations, credit hours, LO mappings, image assignments.

### S1 - Stage 1 Validator (Quality Gate)

Validates A0 + A1 outputs against the **active rule pack** before content generation begins. In **`pipeline.py`**, S1 runs **once per cycle** after A1; if **blocked**, the next cycle starts again from **A0**.

#### Checks Performed

| Category          | Check                   | Rule Source                                         | Severity |
| ----------------- | ----------------------- | --------------------------------------------------- | -------- |
| A0 Metadata       | Title exists            | A0 extraction                                       | Blocker  |
| A0 Metadata       | Course ID exists        | A0 extraction                                       | Warning  |
| A0 Metadata       | LOs extracted (>= 1)    | `content_rules.must_map_to_learning_objectives`     | Blocker  |
| A0 Metadata       | Content sample length   | A0 classification quality                           | Warning  |
| A0 Classification | Confidence >= 0.7       | A0 classification                                   | Warning  |
| A0 Classification | Rule pack resolved      | A0 rule resolution                                  | Blocker  |
| A0 Images         | Image extraction status | A0 image extraction                                 | Info     |
| A1 Structure      | Sections > 0            | A1 parse_document                                   | Blocker  |
| A1 Structure      | Word count > 100        | A1 structural integrity                             | Blocker  |
| A1 Structure      | Section headings exist  | `content_rules.maintain_section_boundary_integrity` | Warning  |
| A1 Word Count     | Lectora page limits     | `lectora_constraints.max_words_per_page`            | Info     |
| A1 KC Count       | KC per lesson minimum   | `kc_placement_rules.min_kc_per_lesson`              | Warning  |
| A1 LO Coverage    | All LOs mapped          | `content_rules.must_map_to_learning_objectives`     | Warning  |
| Assessment        | T/F ban confirmed       | `assessment_rules.allow_true_false`                 | Info     |
| Assessment        | "All above" ban         | `assessment_rules.allow_all_of_the_above`           | Info     |
| Assessment        | Objective coverage      | `assessment_rules.objective_coverage_required`      | Warning  |

**Severity levels:**

- **Blocker** — counts toward S1 **blocked**; triggers another full **A0 → A1 → S1** cycle (until max cycles), then stops A2 if still blocked
- **Warning** — flagged for review; A2 can proceed once S1 is not blocked
- **Info** — informational only

### Section Mapper

Runs after S1 passes. Groups `course_spec` sections under their L1 chapter headings, then assigns those groups sequentially to TO outline lessons from `llm_to_outline`.

**Output shape** (`{stem}_enriched_sections.json`): a list of TO lesson objects, each with its matched `course_spec` sections nested under `subtopics`:

```json
{
  "enriched_sections": [
    {
      "title": "1.0 Coverage, Limits, and Rates",
      "content": "",
      "word_count": "4115",
      "minutes": "23",
      "credit_hour": ".46",
      "interactive_elements": ["bulleted lists", "knowledge checks"],
      "subtopics": [
        {
          "title": "1.1 Coverage",
          "id": "sec_1_1",
          "has_knowledge_check": false,
          "para_start": 28,
          "para_end": 29
        },
        {
          "title": "1.2 Coverage Limits",
          "id": "sec_1_2",
          "has_knowledge_check": true,
          "para_start": 30,
          "para_end": 43,
          "maps_to_objectives": [0, 1]
        }
      ]
    }
  ]
}
```

- L1 headings that duplicate the TO lesson title are deduplicated (not emitted as a subtopic).
- Empty arrays (`subtopics`, `maps_to_objectives`, `images`) and zero `image_count` are omitted to keep the JSON concise.

**Written to:** `shared_state["agent_outputs"]["section_map"]["enriched_sections"]` and `{stem}_enriched_sections.json`.

### A2 - Content Generator

Generates study guide content **one lesson at a time**: all subtopics within a lesson are sent in a **single LLM call** and the LLM returns a **JSON array** — one element per subtopic, in order.

#### Generation Flow

```
For each TO lesson in enriched_sections:
  1. Load full source text for every subtopic (no truncation)
  2. Distribute lesson word_count proportionally across subtopics
     based on each subtopic's source text length
  3. generate_lesson() → ONE LLM call → JSON array of section objects
  4. Count words, attach metadata, append to output
```

#### Per-Lesson LLM Call Context

| Context             | Source                                                              |
| ------------------- | ------------------------------------------------------------------- |
| Lesson spec         | TO lesson title, description, word budget, interactive elements     |
| All subtopic specs  | Heading, word_count target, source text (full, no truncation), KCs  |
| Rule constraints    | `get_rule_pack(...)` from `rule_pack_config/rule_packs.py`          |
| Prior lesson summary| Headings of already-generated sections (never full regenerated text)|

#### Word Count

- Each subtopic's `word_count` is its **proportional share** of the lesson's total `word_count` from `enriched_sections.json`, based on the ratio of its source text length to the lesson's total source text length.
- No hardcoded tolerance bands — the LLM is instructed to match the word count exactly.
- Retries (up to 3×) happen only on JSON parse failure, not on word-count deviation.

#### Rule Pack Enforcement

Content generation follows the dict from **`get_rule_pack(rule_family)`** (`rule_pack_config/rule_packs.py`):

```
style_constraints:     Grade 10-12, second person, short paragraphs, bold key terms
compliance_elements:   Forbidden phrases, non-advisory language, no hallucinated citations
content_rules:         Map to LOs, no duplicate concepts, no unverified statistics
kc_placement_rules:    End of subtopic, 4 options (A-D), no T/F, no "All of the above"
lectora_constraints:   Max 180 words/page, prefer bullets, avoid large text blocks
```

#### Output Document Style

The generated `.docx` matches the reference document format (`IAR_3940_SG`):

| Element           | Style                                                           |
| ----------------- | --------------------------------------------------------------- |
| Title             | Antique Olive Roman, 26pt, deep purple `#3A0A5A`, right-aligned |
| Heading 1         | White text on purple `#9B85B5` background with borders          |
| Heading 2         | Antique Olive, 14pt bold, dark navy `#052A65`                   |
| Heading 3         | Antique Olive, 12pt bold, dark navy, 0.32in indent              |
| Body text         | Palatino Linotype, 11pt, 2in left indent                        |
| Bullet lists      | Same as body, with List Bullet style                            |
| Important callout | Lavender `#DDD6E6` background, navy `#002060` text              |
| Knowledge Check   | Decorative top/bottom borders `#3A0A5A`, A/B/C/D options        |
| Images            | Centered, original aspect ratio, caption below (doc text only)  |

---

## Project Structure

```
Lactora/
+-- .env                          # AZURE_OPENAI_* configuration
+-- pipeline.py                   # run_pipeline(docx, to_outline_doc); A2 optional
+-- requirements.txt              # Python dependencies
+-- doc/                          # Input documents
|   +-- *.docx                    # Source study guides
+-- rule_pack_config/             # Rule packs + timed-outline JSON template
|   +-- rule_packs.py             # RULE_PACKS, get_rule_pack, supplementary notes
|   +-- timed_outline.py          # TO_outline_format for A0 TO LLM extraction
+-- models/                       # Pydantic schemas (request_spec, validation, content, etc.)
+-- shared_llm_config/            # Shared LLM client
|   +-- llm.py                   # AzureOpenAI chat client (all agents)
+-- shared_state/                 # Output directory (simulated Blob storage)
|   +-- {stem}_shared_state.json
|   +-- {stem}_request_spec.json
|   +-- {stem}_provenance_log.json
|   +-- {stem}_llm_to_outline.json # A0: outline LLM output (sidecar)
|   +-- {stem}_course_spec.json
|   +-- {stem}_s1_validation.json
|   +-- {stem}_enriched_sections.json  # Section Mapper sidecar
|   +-- {stem}_generated_content.json
|   +-- {stem}_study_guide.docx
|   +-- {stem}_images/             # Extracted image binaries
+-- agent/
    +-- a0_request_synthesizer/
    |   +-- main.py               # A0RequestSynthesizer class
    |   +-- prompt/
    |   |   +-- classification.py # LLM classification prompt
    |   +-- utils/
    |       +-- doc_parser.py     # CourseDocParser (metadata + image extraction)
    |       +-- classifier.py     # classify_with_llm, classify_to_outline_with_llm, resolve_value
    +-- a1_outline_interpreter/
    |   +-- main.py               # LangGraph nodes, build_graph(), run()
    |   +-- config/
    |   |   +-- llm.py            # Azure OpenAI config
    |   +-- prompt/
    |   |   +-- enrichment.py     # Section enrichment prompt
    |   +-- utils/
    |       +-- helpers.py        # count_words, words_to_minutes, to_snake
    |       +-- image_mapper.py   # map_images_to_sections()
    +-- s1_validator/
    |   +-- main.py               # S1Validator class
    |   +-- utils/
    |       +-- checks.py         # 9 validation check functions
    +-- section_mapper/
    |   +-- main.py               # TO outline ↔ course_spec grouping
    +-- a2_content_generator/
        +-- main.py               # A2ContentGenerator class
        +-- config/
        |   +-- llm.py            # Azure OpenAI config (agent-specific)
        |   +-- styles.py         # DOCX style definitions + helper functions
        +-- prompt/
        |   +-- section_prompt.py # LESSON_SYSTEM prompt + build_lesson_user_message()
        +-- utils/
            +-- source_chunker.py # Full section text extraction + prior summary
            +-- content_writer.py # Lesson-batch generation (generate_lesson, generate_all_sections)
            +-- doc_formatter.py  # Styled .docx rendering (reference doc format)
```

A2 loads the active rule dict with **`get_rule_pack(rule_family)`** from [`rule_pack_config/rule_packs.py`](rule_pack_config/rule_packs.py). Word counts and lesson targets come exclusively from `enriched_sections.json`.

---

## Shared State

All agents read from and write to a single `shared_state.json` file per pipeline run. This acts as simulated Blob storage.

```json
{
  "run_id": "4e781b6a",
  "status": "A1_complete",
  "request_spec": { ... },
  "provenance_log": { ... },
  "extracted_inputs": {
    "title": "...",
    "course_id": "533",
    "learning_objectives": [...],
    "content_sample": "..."
  },
  "images": [...],
  "llm_classification": { ... },
  "llm_to_outline_classification": { ... },
  "s1_validation": { ... },
  "agent_outputs": {
    "A1": { "course_spec": {...}, "inconsistencies": [...] },
    "A2": { ... },
    "A3": null,
    "A4": null,
    "A5": null
  }
}
```

---

## Image Handling Policy

Images follow a strict no-hallucination policy across the pipeline:

| Stage | Action                                         | Rule                                                                                             |
| ----- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| A0    | Extract binary + position + caption + alt_text | Caption from doc text only. Alt_text blanked if contains "AI-generated content may be incorrect" |
| A1    | Map to sections by paragraph index range       | Pure code, no LLM. Unmatched images go to "unassigned"                                           |
| A2    | Insert into .docx at section end               | Original aspect ratio, capped at 4.5in width. Caption below if present                           |
| LLM   | Explicitly instructed                          | "Do not describe visual details unless provided in caption"                                      |

---

## Quick Start

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Set up environment

Create a `.env` file in the project root:

```
AZURE_OPENAI_API_KEY=your_azure_openai_key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_VERSION=2024-12-01-preview
```

### 3. Place your input documents

Put the **study guide** `.docx` and the **timed outline (TO)** `.docx` in `doc/` (or pass absolute paths). A0 reads both: the study guide for metadata/images/classification, the TO file for the outline LLM.

### 4. Run the pipeline

```bash
python3 pipeline.py
```

Defaults are set in `if __name__ == "__main__"` in [`pipeline.py`](pipeline.py): `docx` (study guide) and `to_outline_doc_path` (TO). From code:

```python
from pipeline import run_pipeline

run_pipeline(
    docx_path="doc/your_course.docx",
    to_outline_doc_path="doc/your_course_TO.docx",
)
```

`run_pipeline` prints progress to stdout; artefacts live under `shared_state/` (see below).

### 5. Check outputs

Typical files in `shared_state/` (prefix `{stem}` = `course_id` if set, else `run_id`):

- `{stem}_shared_state.json` — full run state
- `{stem}_request_spec.json`, `{stem}_provenance_log.json` — A0
- `{stem}_llm_to_outline.json` — A0 sidecar: `timestamp`, `run_id`, `course_id`, and `llm_to_outline` (same object as `shared_state.llm_to_outline_classification`)
- `{stem}_course_spec.json` — A1
- `{stem}_s1_validation.json` — S1
- `{stem}_enriched_sections.json` — Section Mapper (enriched course_spec rows for A2)
- `{stem}_study_guide.docx`, `{stem}_generated_content.json` — after A2 is enabled and run
- `{stem}_images/` — images extracted from the study guide

---

## Tech Stack

| Component        | Technology                                          |
| ---------------- | --------------------------------------------------- |
| LLM (chat)       | Azure OpenAI |
| Agent framework  | LangGraph (A1), LangChain (A2)                      |
| Document parsing | python-docx                                         |
| Image extraction | zipfile + ElementTree (raw OOXML)                   |
| Environment      | python-dotenv                                       |
| Language         | Python 3.12+                                        |

---

## Rule Pack Reference

The active rule pack (from `rule_pack_config/rule_packs.py`, chosen by A0 classification) controls assessment and content constraints for generation and validation:

```
InsuranceCE v1.0-dev-flood

assessment_rules:
  - 15 min final exam questions, 4 options (A-D)
  - No True/False, no "All of the above"
  - Rationale required, objective coverage required

style_constraints:
  - Grade 10-12 reading level
  - Second person voice, neutral instructional tone
  - Max 5 sentences per paragraph, bold first key term

compliance_elements:
  - Disclosure handling + allowed references are defined by the rule pack (do not assume generic forbidden phrases unless present in the pack)

kc_placement_rules:
  - End of subtopic, 2-5 per lesson
  - Not mid-paragraph, after table, or inside regulatory block
  - Plausible distractors, explanation required

lectora_constraints:
  - Max 180 words per page
  - Prefer bulleted content, subtopic-based page breaks
```
