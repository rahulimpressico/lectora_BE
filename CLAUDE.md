# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in all values
alembic upgrade head   # required before starting API or worker
```

### Entry Points

| Scenario | Command |
|----------|---------|
| Production API | `uvicorn lectora_backend.main:app --reload` |
| Worker (separate terminal) | `PYTHONUNBUFFERED=1 python -m lectora_backend.worker` |
| Local dev (full pipeline, no DB/Bus/Blob) | `uvicorn lectora_backend.dev_app:app --reload` |
| Direct pipeline (no API/worker) | `python3 lectora_backend/pipeline/pipeline.py` |

`dev_app.py` supports the full frontend workflow (upload → generate-TO → three-panel → pipeline → course editor) using an in-memory job store and local filesystem. Requires only `AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_ENDPOINT`. Artifacts are written to `pipeline/courses/{courseSlug}/`.

### Docker

**Dev** (`dev_app` — full frontend workflow, no Service Bus or Blob Storage):
```bash
cp .env.example .env   # AZURE_OPENAI_* required at minimum
docker compose up --build -d
# API: http://localhost:8000/docs
```

**Production** (`main.py` + worker — all Azure services required):
```bash
docker compose -f docker-compose.prod.yml up --build -d
```

Both compose files default to `DATABASE_URL=sqlite:////app/data/lectora.db` (persisted volume). The prod compose sets `RUN_MIGRATIONS=1` on the API container so Alembic runs on startup; the dev compose sets it to `0`.

### Database migrations
```bash
alembic upgrade head                              # apply all migrations
alembic current                                   # show current revision
alembic revision --autogenerate -m "description"  # create migration after model changes
```

### Integration smoke test (requires API + worker running)
```bash
python -m pytest lectora_backend/tests/integration/test_job_flow_smoke.py -v
```

## Architecture Overview

This system has two execution modes that share the same pipeline agents:

### Mode 1: Backend API + Worker (production path)
`POST /jobs` → Azure Service Bus → Worker consumes message → runs pipeline → uploads artifacts to Azure Blob Storage → marks job `COMPLETED` in SQL.

- **`lectora_backend/main.py`** — FastAPI app; validates env vars, DB, Blob, and Service Bus on startup
- **`lectora_backend/worker.py`** — Worker; listens on Service Bus queue
- **`lectora_backend/core/orchestrator.py`** — Worker message loop + gate retry logic (`MAX_S1_GATE_CYCLES = 3`); renews Service Bus lock (30 min max)
- **`lectora_backend/core/pipeline_adapter.py`** — Bridge that translates backend shared state into pipeline agent calls and merges outputs back
- **`lectora_backend/core/state_manager.py`** — Reads/writes `shared_state.json` to Azure Blob
- **`lectora_backend/repositories/`** — SQL CRUD (`job_repository.py`) and Blob Storage wrapper (`blob_repository.py`)

### Mode 2: Local dev (`dev_app` + in-memory store)
`POST /jobs` → `local_course_job_store` (in-memory, 2 hr TTL, max 3 concurrent) → runs same pipeline agents → writes artifacts to `pipeline/courses/{courseSlug}/`.

- **`lectora_backend/api/routes/local_jobs.py`** — Job lifecycle routes for dev mode
- **`lectora_backend/api/local_course_job_store.py`** — TTL-based in-memory store; replaces SQL for local execution
- **`lectora_backend/core/blob_resolver.py`** — Resolves blob paths to local files; downloads from Azure and caches to disk on first access to avoid re-downloads
- **`lectora_backend/core/job_registry.py`** — Thread-safe registry of in-flight jobs; enables `/storage/delete` to cancel running jobs that touch deleted paths

### Mode 3: Direct pipeline execution
`python pipeline.py` calls `run_pipeline(docx_path, to_outline_doc_path)` directly, writing artifacts to `pipeline/shared_state/`.

### Pipeline Agent Flow

```
A0 → A1 → [S1 gate: up to 3 cycles] → Section Mapper → KC Planner → A2 → [S2 gate: up to 3 cycles] → study_guide.docx
```

Each agent lives in `lectora_backend/pipeline/agent/<name>/main.py` and follows a consistent layout:

```
agent/<name>/
  main.py          ← agent entry point
  config/llm.py    ← model name, temperature, token limits
  utils/           ← pure helper functions (no LLM calls)
  prompt/          ← LLM prompt strings (modify here to tune behavior)
```

| Agent | Role |
|-------|------|
| **A0** (`a0_request_synthesizer`) | Extracts metadata from primary document (DOCX or PDF), sends timed-outline to LLM for outline extraction, classifies rule family, extracts images; also chunks all source files for downstream retrieval |
| **A1** (`a1_outline_interpreter`) | LangGraph `StateGraph`; parses document structure, maps images, enriches via LLM, builds `course_spec.json` |
| **S1** (`s1_validator`) | Quality gate on A0+A1 outputs; blockers trigger full A0→A1→S1 retry |
| **Section Mapper** (`section_mapper`) | Groups `course_spec` sections under L1 chapters, maps to TO lessons; produces `enriched_sections.json` |
| **KC Planner** (`kc_planner`) | Determines Knowledge Check placement via 3 auto-selected scenarios; mutates `has_knowledge_check` flags on subtopics in `enriched_sections`; outputs `kc_plan.json` |
| **A2** (`a2_content_generator`) | One LLM call per lesson (all subtopics batched); retrieves relevant chunks from source files via BM25-style keyword scoring; renders final styled `.docx` |
| **S2** (`s2_validator`) | Quality gate on generated content; blockers trigger A2 regeneration with feedback |

Shared KC regex patterns (`is_kc_title()`, etc.) live in `pipeline/shared_utils/kc_patterns.py`.

#### Multi-file source support

A0 accepts multiple source files (DOCX + PDF) via the `source_file_paths` field on `POST /jobs`. `a0/utils/chunker.py` splits each file at heading boundaries (~400 words/chunk, 50-word overlap) and stores chunks in shared state. A2 calls `retrieve_chunks_for_topic()` (BM25-style keyword overlap, no vector DB) to pull relevant context per lesson. PDF parsing uses `a0/utils/pdf_extractor.py`; DOCX parsing uses `python-docx`.

#### KC Planner Scenarios (auto-selected)

The KC Planner selects one of three scenarios based on source document content and TO availability:

- **Scenario A** — Raw doc has KCs flagged by A1; if TO present, cross-reference and keep only TO-confirmed KCs. Decisions: `confirmed_by_to` or `removed_not_in_to`.
- **Scenario B** — Raw doc has no KCs but TO is available; derive placements from TO `interactive_elements` or KC-titled subtopics. Decision: `kc_from_to`.
- **Scenario C** — No KCs anywhere and no TO; algorithmic placement using rule pack's `kc_placement_rules` (cadence intervals, forbidden placements, min/max per lesson). Decision: `kc_from_rule_pack`.

### Rule Packs

Rule packs control all content, assessment, and style constraints. They live in `lectora_backend/pipeline/rule_pack_config/packs/` (`insurance_ce.py`, `iarce.py`, `firm_element.py`). A0 classifies the course into a rule family; all downstream agents call `resolve_rule_pack(rule_family)` (in `rule_pack_config/rule_packs.py`) to load constraints.

Rule pack fields: `assessment_rules`, `style_constraints`, `compliance_elements`, `content_rules`, `kc_placement_rules`. `insurance_ce_difficulty.py` adds parameterized difficulty levels.

### API Contract

**Authentication:** Production routes (`main.py`) use Microsoft Entra ID OAuth2 Bearer tokens via `EntraTokenValidator` middleware. `dev_app.py` has no auth.

**`POST /jobs`** — create a job.
```json
{
  "studyGuide": "blob/path/to/study_guide.docx",
  "timedOutline": "blob/path/to/timed_outline.docx",
  "courseTitle": "...",
  "courseType": "insurance_ce",
  "requestedBy": "user@example.com",
  "to_override": { ... },          // optional: user-edited TO JSON from three-panel editor
  "source_file_paths": ["..."]     // optional: additional DOCX/PDF files for multi-file chunking
}
```
Returns `202 Accepted` with `{ "jobId": "..." }`. In production, blobs must already exist in Azure Blob Storage; in dev mode, `blob_resolver.py` downloads and caches them locally.

**`GET /jobs/{jobId}`** — job status, stage progress array, artifact list.

**`GET /jobs/{jobId}/events`** — SSE stream of real-time stage updates and logs.

**`GET /jobs/{jobId}/course`** — course content after completion.

**`GET /jobs/{jobId}/artifacts/download`** — download final `.docx`.

**`POST /jobs/{jobId}/retry`** — retry from a specific stage (production only).

**Document upload + TO generation endpoints** (both `main.py` and `dev_app.py`):

- `POST /documents/upload` — upload a DOCX, PDF, or pre-built JSON timed-outline file; returns the blob path. JSON files skip LLM outline generation in A0.
- `POST /documents/generate-to` — run A0 asynchronously (default) or synchronously (`wait=true`); returns `{ jobId }` for async, full result for sync.
- `GET /documents/generate-to/jobs/{jobId}` — poll async A0 result; status: `pending | running | completed | failed`.

**Settings endpoints** (both `main.py` and `dev_app.py`):

- `GET /settings` — returns current per-agent model configs (default, current, override flag) and available model list.
- `PUT /settings/models` — bulk-update model deployments for one or more agents. Changes apply immediately without server restart; overrides are persisted to `pipeline/shared_llm_config/model_overrides.json`.
- `POST /settings/models/reset` — revert overrides to defaults (pass `{ "agent_id": "A1" }` for a single agent; omit body to reset all).

**Storage endpoints** (both `main.py` and `dev_app.py`):

- `GET /storage/browse?prefix=` — browse artifacts container (Azure or local `pipeline/courses/`)
- `GET /storage/uploaded-documents/browse?prefix=` — browse source documents container
- `GET /storage/file?path=&source=` — download/preview a file
- `POST /storage/delete` — delete files/folders; cancels any in-flight jobs that touch those paths via `job_registry`

Schemas live in `lectora_backend/api/schemas/`.

### Storage Layout

Artifacts are organized by **course slug** (sanitized title), not job ID:

```
{courseSlug}/         ← e.g. Long_Term_Care/
  doc/                ← original input .docx/.pdf files
  output/             ← pipeline artifacts (JSON + final .docx)
  images/             ← extracted image binaries
  logs/               ← stage completion logs
  state/              ← shared_state.json
```

`core/course_storage.py` manages slug generation (`sanitize_course_slug()`), path construction, and backward compatibility with legacy `outputs/` segments. In dev mode this maps to `pipeline/courses/{courseSlug}/`; in production it maps to the Azure Blob container prefix.

Source documents uploaded by the frontend are stored in a separate `uploaded-documents` container/prefix. `core/blob_resolver.py` resolves paths from either container to local disk, caching Azure downloads to avoid re-fetches.

### Database Schema

Three tables managed by Alembic (`alembic/versions/`):

- **`jobs`** — `job_id`, `status` (`PENDING|PROCESSING|COMPLETED|FAILED|CANCELLED`), `course_title`, `course_type`, `requested_by`, `shared_state_blob_path`, `created_at`, `updated_at`
- **`stage_progress`** — `id`, `job_id`, `stage_id` (A0/A1/S1/A2/S2), `status`, `validation_outcome` (`PASS|WARNING|RECOVERABLE_FAIL|CRITICAL_FAIL`), `started_at`, `completed_at`, `error_detail` (JSON)
- **`retry_history`** — `id`, `job_id`, `attempt`, `from_stage`, `triggered_by`, `outcome`

Models: `lectora_backend/models/db_models.py`. Enums: `lectora_backend/models/job_enums.py`.

### Shared State Contract

Agents do not import each other. The orchestrator wires them via a shared state dict (written as `shared_state.json`). Key top-level fields:

- `run_id`, `status` — pipeline run identity and stage
- `extracted_inputs` — A0 metadata (title, course_id, learning_objectives, content_sample)
- `images` — extracted image list with paragraph indices
- `source_chunks` — chunked content from all source files (populated by A0, consumed by A2)
- `llm_classification` — A0 rule family classification result
- `llm_to_outline_classification` — A0 timed-outline LLM extraction
- `agent_outputs.A1.course_spec` — full section hierarchy from A1
- `agent_outputs.section_map.enriched_sections` — Section Mapper output; KC Planner mutates `has_knowledge_check` flags on subtopics in-place
- `agent_outputs.kc_planner` — KC Planner report: `{scenario, decisions[]}` with per-subtopic placement decisions
- `s1_validation`, `s2_validation` — validator reports (`ValidationIssue[]` with `field`, `severity`, `message`, `remediation`)

### S2 Word Count Validation Logic

S2 uses two scenarios based on source document size vs. Timed Outline target:

- **Source ≤ 140% of TO target** — enforces a 50%–80% generation band; outside this range is a blocker.
- **Source > 140% of TO target** — compares generated content directly to the full TO target; over is a blocker, under is a warning.

After the main check passes, a deviation check compares section-level word counts against individual TO lesson targets.

### Model Registry

`pipeline/shared_llm_config/model_registry.py` is the single source of truth for per-agent LLM deployments. Agents call `get_deployment(agent_id)` at call time so settings-API overrides take effect on the next run without a restart.

Default deployments:

| Agent ID | Default deployment | Role |
|----------|--------------------|------|
| `A0` | `o3` | Rule family classification (reasoning model) |
| `A0_TO` | `gpt-5.4` | Timed-outline extraction (large context window) |
| `A1` | `gpt-5.4-mini` | Section enrichment |
| `A2` | `gpt-5.4` | Content generation |

Overrides are written to `pipeline/shared_llm_config/model_overrides.json` and survive server restarts. The settings API (`PUT /settings/models`) is the intended way to change them; direct file edits also work.

### Key Design Rules
- **Parser owns structure** in A1: sections, word counts, KCs come from python-docx parsing. LLM only enriches (adds subtopics, LO mappings).
- **Images follow no-hallucination policy**: extracted by A0, mapped by pure code in A1, inserted at section end by A2. LLM never describes visual content.
- **S1 blockers** restart the entire A0→A1→S1 chain (fresh shared state). S2 blockers regenerate only A2 content.
- **`study_guide.docx` is rendered only after S2 passes** (pass or pass_with_warnings).
- **`DATABASE_URL` must be an absolute path** when using SQLite — relative paths resolve differently from API vs worker processes.
- **Azure OpenAI o-series models require `max_completion_tokens`**, not `max_tokens` — using the wrong parameter silently breaks generation.
- Production: input blobs must already exist in Azure Blob Storage before `POST /jobs` — the API records paths only. Dev mode: `blob_resolver.py` downloads and caches blobs on demand.
- Schema is managed via Alembic; the app does not auto-create tables at runtime.
- Orphaned Service Bus messages (job not found in DB) are dead-lettered immediately, not retried.
- Storage deletes cancel in-flight jobs via `job_registry` before removing files.

### Observability

Langfuse tracing is wired in `pipeline/shared_llm_config/tracer.py`. Call `set_doc_name()` / `set_run_id()` at pipeline start to annotate traces. Requires `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` in `.env`; omitting them disables tracing without breaking the pipeline.

### Required Environment Variables

```env
DATABASE_URL=sqlite:////absolute/path/to/lectora.db   # must be absolute
SERVICE_BUS_NAMESPACE=
SERVICE_BUS_CONNECTION_STRING=
QUEUE_NAME=course-jobs
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_DEPLOYMENT=
AZURE_TENANT_ID=
AZURE_CLIENT_ID=
AZURE_CLIENT_SECRET=
AZURE_AUDIENCE=
AZURE_STORAGE_CONNECTION_STRING=
BLOB_CONTAINER_NAME=
LANGFUSE_PUBLIC_KEY=          # optional tracing
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
```

All required vars are validated at API startup — missing values cause an immediate `RuntimeError`. `dev_app.py` only requires the `AZURE_OPENAI_*` vars.

### CORS

Both `main.py` and `dev_app.py` call `add_cors_middleware(app)` from `api/middleware/cors.py`, which reads `settings.cors_origins_list()`. Override via env vars (defaults shown):

```env
CORS_ORIGINS=http://localhost:5173,http://localhost:3000,http://localhost:8080,https://nimble-cendol-69a81c.netlify.app
CORS_ORIGIN_REGEX=https://.*\.netlify\.app   # covers Netlify branch/preview deploys
```

### Validation Severity Levels
- **Blocker** — pipeline must not proceed; triggers retry then stops
- **Critical** — must review before publishing; does not block pipeline
- **Warning** — flagged for review; pipeline continues
- **Info** — informational only
