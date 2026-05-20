# Lectora Course Generation Engine

Backend service for AI-driven CE course generation. Accepts a study guide and timed outline, runs a multi-agent pipeline, and delivers a fully formatted Word document ready for Lectora publishing.

---

## What This Service Does

- `POST /jobs` to create a course generation job
- SQL persistence for job metadata and stage progress
- Azure Blob Storage persistence for `shared_state.json` and all pipeline artifacts
- Azure Service Bus publishing for new job events
- Worker-side queue consumption and full end-to-end pipeline execution
- Complete pipeline: **A0 → A1 → S1 gate → Section Mapper → KC Planner → A2 → S2 gate → study_guide.docx**

Current worker behavior:

- Looks up the job in SQL first; orphan messages (job missing) are dead-lettered immediately with reason `JobNotFound` — no retry waste
- Sets SQL job status to **`PROCESSING`** during work and **`COMPLETED`** after the final `study_guide.docx` is produced (or **`FAILED`** if any gate exhausts all retries)
- Runs the full pipeline end-to-end with two quality gates:
  - **S1 gate** validates A0 + A1 outputs before content is written — up to 3 cycles with feedback
  - **S2 gate** validates generated content before the final DOCX is assembled — up to 3 cycles with feedback
- The final `study_guide.docx` is only rendered after S2 passes

---

## Pipeline Agent Flow

```
A0 → A1 → [S1 gate: up to 3 cycles] → Section Mapper → KC Planner → A2 → [S2 gate: up to 3 cycles] → study_guide.docx
```

| Agent | Role |
|---|---|
| **A0** — Request Synthesizer | Extracts title, course ID, learning objectives, and 8,000 words of body content from the study guide. Sends timed outline to LLM for lesson extraction. Classifies rule family. Extracts images. |
| **A1** — Outline Interpreter | LangGraph StateGraph. Parses document structure, maps images, enriches sections via LLM, builds `course_spec.json`. |
| **S1** — Blueprint Validator | Quality gate on A0 + A1 outputs. Checks course basics, classification confidence, outline structure, KC counts, and LO coverage. Blockers trigger a full A0 → A1 → S1 retry (up to 3 cycles). |
| **Section Mapper** | Groups `course_spec` sections under L1 chapters and maps them to TO lessons. Produces `enriched_sections.json`. |
| **KC Planner** | Determines which lessons receive a knowledge check. Three scenarios: raw doc has KCs (cross-references with TO), no KCs but TO present (derives from TO), no KCs and no TO (uses rule-pack cadence). Updates `enriched_sections` KC flags before A2 runs. |
| **A2** — Content Generator | One LLM call per lesson (all subtopics batched). Uses 8,000 words of source content + learning objectives + course title for the course description. Defers DOCX rendering until S2 passes. |
| **S2** — Content Validator | Quality gate on generated content. Checks completeness, quiz structure, writing style, course structure, and word count against the timed outline target. Blockers trigger A2 regeneration with feedback (up to 3 cycles). |

### Rule Packs

All compliance, assessment, and style constraints are driven by rule packs in `lectora_backend/pipeline/rule_pack_config/packs/`:

| Pack | Course Type |
|---|---|
| `insurance_ce` | Insurance continuing education |
| `iarce` | Investment adviser representative CE |
| `firm_element` | Firm element training |

A0 classifies the course into a rule family; all downstream agents load constraints from the resolved rule pack.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          CLIENT / POSTMAN                               │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │  POST /jobs  (studyGuide + timedOutline blobPaths)
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         FASTAPI  (main.py)                              │
│  1. Validate request                                                    │
│  2. Build blob layout → {jobId}/{fileName}/                             │
│  3. Stage SQL job record                                                │
│  4. Write shared_state.json → Azure Blob Storage                        │
│  5. Commit SQL transaction                                              │
│  6. Publish JOB_CREATED → Azure Service Bus                             │
│  7. Return 202 Accepted → { jobId, status: PENDING }                   │
└──────────┬──────────────────────────────────┬───────────────────────────┘
           │                                  │
           ▼                                  ▼
┌──────────────────┐               ┌──────────────────────┐
│  SQLite / Azure  │               │  Azure Blob Storage  │
│  SQL Database    │               │  {jobId}/{fileName}/ │
└──────────────────┘               └──────────────────────┘
                                                      │
┌─────────────────────────────────────────────────────▼───────────────────┐
│                    AZURE SERVICE BUS  (Queue: course-jobs)              │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │  JOB_CREATED message  { jobId }
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         WORKER  (worker.py)                             │
│                                                                         │
│  receive message → parse jobId → SQL lookup                             │
│  → mark PROCESSING → load shared_state.json from Blob                  │
│  → download inputs to temp dir                                          │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────┐            │
│  │  S1 Gate Loop (max 3 cycles)                            │            │
│  │  A0 → A1 → S1                                          │            │
│  │  blocked/blocker? → retry with S1 feedback             │            │
│  │  pass/warn → proceed                                    │            │
│  └───────────────────────────┬─────────────────────────────┘            │
│                              │                                          │
│                    Section Mapper → KC Planner                          │
│                              │                                          │
│  ┌─────────────────────────────────────────────────────────┐            │
│  │  S2 Gate Loop (max 3 cycles)                            │            │
│  │  A2 (content generation, DOCX deferred)                 │            │
│  │  S2 (content validation)                                │            │
│  │  blocked? → retry A2 with S2 feedback                  │            │
│  │  pass/warn → render study_guide.docx                   │            │
│  └───────────────────────────┬─────────────────────────────┘            │
│                              │                                          │
│  upload artifacts to Blob → mark COMPLETED                              │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Blob Storage Layout (per job)

```
Azure Blob Container
└── {jobId}/                          e.g.  j-85d575b4/
    └── {fileName}/                          study_guide/
        ├── doc/
        │   ├── study_guide.docx             ← original input
        │   └── timed_outline.docx           ← original input
        ├── output/
        │   ├── request_spec.json            ← A0 output
        │   ├── provenance_log.json          ← A0 output
        │   ├── llm_to_outline.json          ← A0 timed-outline extraction
        │   ├── course_spec.json             ← A1 output
        │   ├── s1_validation.json           ← S1 report
        │   ├── kc_plan.json                 ← KC Planner output
        │   ├── enriched_sections.json       ← Section Mapper output
        │   ├── generated_content.json       ← A2 output
        │   ├── s2_validation.json           ← S2 report
        │   └── study_guide.docx             ← final generated course (rendered after S2 passes)
        ├── images/
        │   └── <extracted image files>
        ├── logs/
        │   └── a1_complete.json             ← or a1_failed / a1_stopped
        └── state/
            ├── shared_state.json            ← backend runtime state
            └── pipeline_shared_state.json   ← pipeline agent state
```

---

## Validation & Quality Gates

### S1 — Blueprint Check (runs before content is written)

Validates A0 and A1 outputs before any content is generated:

| What is checked | Severity if failed |
|---|---|
| Course title extracted | Blocker |
| Learning objectives present | Blocker |
| Rule pack resolved | Blocker |
| Timed outline present (when required) | Blocker |
| LO count within rule-pack range | Blocker |
| Outline has sections with word counts | Blocker |
| Classification confidence ≥ 0.7 | Warning |
| Quiz question minimums met | Warning |
| All LOs mapped to at least one section | Warning |

### S2 — Content Check (runs after content is written)

Validates the generated course content before the DOCX is assembled:

| Category | What is checked |
|---|---|
| Completeness | All sections generated, no failures |
| Quiz quality | Answer counts, correct answer, explanation, no T/F or AOTA if banned |
| Writing style | No forbidden phrases, correct voice (2nd/3rd person per rule pack), no prohibited source citations |
| Course structure | Intro section, summary section, no duplicate headings, LOs covered |
| Word count | Total within bounds vs Timed Outline target (see below) |

### Word Count Validation Logic

The S2 word count check uses two scenarios based on source document size:

- **Source ≤ 140% of TO target** — enforces a 50%–80% generation band; outside this range is blocked
- **Source > 140% of TO target** — compares directly to the full TO target; over is blocked, under is a warning

After the main check passes, a deviation check compares section-level word counts against individual TO lesson targets.

### Severity Levels

| Level | Effect |
|---|---|
| 🔴 Blocker | Pipeline stops; triggers retry then halts if limit reached |
| 🟠 Critical | Document may be created but mandatory review required |
| 🟡 Warning | Pipeline continues; issue flagged for review |
| 🔵 Info | Awareness only; no action required |

---

## Content Generation Details

### Course Description

A2 generates the course overview description (OVERVIEW section in DOCX) using:
- Course title
- All learning objectives
- Up to 8,000 words of raw body content extracted from the source document
- Target: ~120 words (110–130 range)

### Learning Objectives Extraction

A0 extracts learning objectives from the source document using:
- Bullet characters (`•`, `–`, `·`, `*`)
- Numbered list styles (`1.`, `(a)`, etc.)
- Word paragraph styles: `List Bullet`, `List Number`, `List Number 2`, `List Paragraph`

### Knowledge Check Placement

The KC Planner runs three scenarios automatically:
- **Scenario A** — Source doc has KCs: uses raw doc flags, cross-referenced with TO if available
- **Scenario B** — No KCs in source, TO present: derives KC placement from TO interactive elements
- **Scenario C** — No KCs, no TO: algorithmically places KCs using rule-pack cadence rules

---

## Tech Stack

- FastAPI
- SQLAlchemy + Alembic
- Azure Blob Storage
- Azure Service Bus
- Azure OpenAI (GPT deployment, uses `max_completion_tokens`)
- LangGraph (A1 outline interpreter)
- python-docx (document parsing and DOCX generation)
- SQLite (local dev) / Azure SQL (production)

---

## Repository Structure

```text
lectora_backend/
  api/
    routes/              — FastAPI endpoints (jobs, health)
    schemas/             — Pydantic request/response models
    middleware/          — Logging, auth middleware
  core/
    blob_layout.py       — Blob prefix builder ({jobId}/{fileName}/...)
    orchestrator.py      — Worker message loop + S1/S2 gate orchestration
    pipeline_adapter.py  — Bridge between backend state and pipeline agents
    queue_publisher.py   — Azure Service Bus sender
    state_manager.py     — shared_state.json read/write
    audit_logger.py      — Structured audit log helper
  models/
    db_models.py         — SQLAlchemy ORM models
    job_enums.py         — JobStatus, PipelineStep, StageStatus, etc.
  repositories/
    job_repository.py    — SQL CRUD for jobs and stage progress
    blob_repository.py   — Azure Blob Storage wrapper
  pipeline/
    agent/
      a0_request_synthesizer/   — Document parsing + LLM classification
      a1_outline_interpreter/   — LangGraph outline builder
      a2_content_generator/     — LLM content generation + DOCX assembly
      kc_planner/               — Knowledge check placement logic
      s1_validator/             — Blueprint quality gate
      s2_validator/             — Content quality gate
      section_mapper/           — Maps course_spec sections to TO lessons
    rule_pack_config/           — Rule packs (insurance_ce, iarce, firm_element)
    shared_llm_config/          — Shared Azure OpenAI client + tracer
    shared_state/               — Local pipeline run artifacts (dev only)
  tests/
    integration/
  main.py       — FastAPI app entrypoint
  worker.py     — Worker process entrypoint
alembic/
alembic.ini
requirements.txt
.env
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in all values. The app will refuse to start if any required variable is empty.

```env
# Database — use absolute path for SQLite to avoid API/worker cwd mismatch
DATABASE_URL=sqlite:////absolute/path/to/lectora.db

# Azure Service Bus
SERVICE_BUS_NAMESPACE=
SERVICE_BUS_CONNECTION_STRING=
QUEUE_NAME=course-jobs

# Azure OpenAI
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_DEPLOYMENT=

# Microsoft Entra ID (auth)
AZURE_TENANT_ID=
AZURE_CLIENT_ID=
AZURE_CLIENT_SECRET=
AZURE_AUDIENCE=

# Azure Blob Storage
AZURE_STORAGE_CONNECTION_STRING=
BLOB_CONTAINER_NAME=

# Langfuse (optional tracing)
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
```

> **Important:** Use an **absolute path** for `DATABASE_URL` when using SQLite. A relative path resolves differently depending on which directory the API or worker process starts from.

---

## Setup

### 1. Create and activate virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in all required values
```

### 4. Apply database migrations

```bash
alembic upgrade head
```

This is required before starting the API or worker.

### 5. Upload input files to Blob Storage

Before creating a job, upload the input `.docx` files to your blob container at the paths you plan to reference in the payload:

```
Container: regedlectoraaistorage
  uploaded-documents/flood/study_guide.docx
  uploaded-documents/flood/timed_outline.docx
```

---

## Running the System

### API + Worker (production path)

```bash
# Terminal 1 — API
uvicorn lectora_backend.main:app --reload

# Terminal 2 — Worker
PYTHONUNBUFFERED=1 python -m lectora_backend.worker
```

### Direct pipeline execution (local / dev path)

No API or worker needed:

```bash
python3 lectora_backend/pipeline/pipeline.py
```

Artifacts are written to `lectora_backend/pipeline/shared_state/`.

---

## API Endpoints

### Health check

```http
GET /health/
```

Response: `{ "status": "ok" }`

### Create job

```http
POST /jobs
```

**Payload:**

```json
{
  "courseTitle": "Enhanced Flood Insurance",
  "courseType": "compliance",
  "inputs": {
    "courseBrief": null,
    "timedOutline": { "blobPath": "uploaded-documents/flood/timed_outline.docx" },
    "studyGuide":   { "blobPath": "uploaded-documents/flood/study_guide.docx" },
    "examReference": null,
    "complianceNotes": null
  }
}
```

`studyGuide.blobPath` and `timedOutline.blobPath` are both required. All blobs must already exist in the container before creating a job.

**curl example:**

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "courseTitle": "Enhanced Flood Insurance",
    "courseType": "compliance",
    "inputs": {
      "courseBrief": null,
      "timedOutline": {"blobPath": "uploaded-documents/flood/timed_outline.docx"},
      "studyGuide":   {"blobPath": "uploaded-documents/flood/study_guide.docx"},
      "examReference": null,
      "complianceNotes": null
    }
  }'
```

**Response:** `{ "jobId": "j-xxxxxxxx", "status": "PENDING" }`

### Get job status

```http
GET /jobs/{jobId}
```

**Response (completed job):**

```json
{
  "jobId": "j-xxxxxxxx",
  "status": "COMPLETED",
  "createdAt": "2026-05-13T06:42:38.000000",
  "updatedAt": "2026-05-13T07:00:00.000000",
  "stages": [
    { "stage": "A0", "status": "COMPLETED" },
    { "stage": "A1", "status": "COMPLETED" },
    { "stage": "S1", "status": "COMPLETED", "outcome": "PASS" },
    { "stage": "A2", "status": "COMPLETED", "outcome": "PASS" }
  ],
  "error": null
}
```

### Retry job

```http
POST /jobs/{jobId}/retry
```

### List artifacts

```http
GET /jobs/{jobId}/artifacts
```

---

## Alembic Commands

```bash
alembic upgrade head                              # apply all migrations
alembic current                                   # show current revision
alembic revision --autogenerate -m "description"  # create migration after model changes
```

---

## Operational Notes

- Do not commit `.env` or `lectora.db`
- `DATABASE_URL` must be an absolute path when using SQLite
- Input blobs must exist in the container before `POST /jobs` — the API records paths only
- `study_guide.docx` is only produced after S2 passes (pass or pass_with_warnings)
- S1 gate retries are capped at `MAX_S1_GATE_CYCLES = 3` (see `lectora_backend/core/orchestrator.py`)
- S2 gate retries are capped at `MAX_A2_S2_CYCLES = 3` (see `lectora_backend/core/pipeline_adapter.py` for production, `lectora_backend/pipeline/pipeline.py` for local dev)
- A2 content is regenerated with S2 feedback on each retry cycle
- Section Mapper runs before KC Planner — KC Planner mutates the `enriched_sections` that Section Mapper produces
- S2 runs inside the A2 stage; it is not tracked as a separate pipeline stage in the job status response
- Blob shared state is the runtime source of truth; SQL stores metadata for API visibility
- Service Bus messages are completed only on confirmed success; failures are abandoned or dead-lettered
- The worker deletes its per-job temp directory after each run
- All required config is validated at API startup — missing values cause an immediate `RuntimeError`
- Azure OpenAI models that use the o-series API require `max_completion_tokens` (not `max_tokens`)

---

## Quick Start Checklist

```bash
source .venv/bin/activate
pip install -r requirements.txt
# Edit .env — set absolute DATABASE_URL and all Azure credentials
alembic upgrade head
# Upload input .docx files to blob container
uvicorn lectora_backend.main:app --reload
# In another terminal:
PYTHONUNBUFFERED=1 python -m lectora_backend.worker
# Create a job:
curl -X POST http://127.0.0.1:8000/jobs -H "Content-Type: application/json" -d @payload.json
# Poll for status:
curl http://127.0.0.1:8000/jobs/<jobId>
```
