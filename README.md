# Lectora Course Generation Engine

A production-grade AI pipeline that transforms source study guides and timed outlines into fully formatted, compliance-ready continuing education course documents. The system combines a FastAPI backend, a multi-agent LLM pipeline, and Azure AI Search vector retrieval to produce structured Word documents ready for Lectora publishing.

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Repository Layout](#repository-layout)
3. [Architecture Overview](#architecture-overview)
4. [Working Architecture Flow](#working-architecture-flow)
5. [Architecture Flow (Step by Step)](#architecture-flow-step-by-step)
6. [Pipeline Agents](#pipeline-agents)
7. [Ingestion Pipeline](#ingestion-pipeline)
8. [Vector Retrieval System](#vector-retrieval-system)
9. [How Azure AI Search Works](#how-azure-ai-search-works)
10. [Azure Services](#azure-services)
11. [Execution Modes](#execution-modes)
12. [API Endpoints](#api-endpoints)
13. [Rule Packs](#rule-packs)
14. [Knowledge Check Planner](#knowledge-check-planner)
15. [Environment Variables](#environment-variables)
16. [Local Development Setup](#local-development-setup)
17. [Docker Deployment](#docker-deployment)
18. [Database Migrations](#database-migrations)
19. [Running Tests](#running-tests)

---

## Project Overview

The engine accepts one or more source DOCX/PDF files and a Timed Outline document, then runs a gated multi-agent pipeline that:

1. Parses and classifies course structure from raw documents
2. Validates structure and content against compliance rule packs
3. Indexes source content into Azure AI Search using 3072-dimensional embeddings
4. Retrieves semantically relevant content for each section via vector similarity search
5. Generates lesson-by-lesson course content via Azure OpenAI
6. Validates generated content before assembling the final styled DOCX

The system supports three course families — `insurance_ce`, `iarce`, and `firm_element` — each with its own assessment rules, style constraints, and KC placement logic. Difficulty levels (`basic`, `intermediate`, `advanced`) apply NAIC CE credit-hour multipliers to word-count targets.

---

## Repository Layout

```
lectora-course-gen-engine/
├── alembic/                        ← Database migration scripts
├── alembic.ini
├── docker/                         ← Docker helper files
├── docker-compose.yml              ← Dev mode (dev_app, no worker)
├── docker-compose.prod.yml         ← Production mode (API + worker)
├── Dockerfile
├── requirements.txt
├── .env.example                    ← Copy to .env and fill all values
│
└── lectora_backend/
    ├── main.py                     ← Production FastAPI app (auth + Azure)
    ├── dev_app.py                  ← Dev FastAPI app (no auth, local FS)
    ├── worker.py                   ← Azure Service Bus consumer
    ├── config.py                   ← Pydantic settings from .env
    │
    ├── api/
    │   ├── routes/
    │   │   ├── generate_to.py      ← Upload, generate-TO, ingestion status
    │   │   ├── local_jobs.py       ← Job CRUD for dev mode
    │   │   └── ...
    │   ├── schemas/                ← Pydantic request/response models
    │   ├── ingestion_status_store.py  ← In-memory upload status (4 hr TTL)
    │   └── local_course_job_store.py  ← In-memory job store for dev mode
    │
    ├── core/
    │   ├── orchestrator.py         ← Worker message loop + gate retry logic
    │   ├── pipeline_adapter.py     ← Translates shared_state ↔ pipeline calls
    │   ├── state_manager.py        ← Reads/writes shared_state.json to Blob
    │   ├── blob_layout.py          ← Deterministic blob prefix builder
    │   ├── blob_resolver.py        ← Resolves blob paths → local disk (with cache)
    │   ├── azure_course_artifacts.py  ← Reads artifacts from Azure when enabled
    │   └── local_artifact_sync.py  ← Uploads local dev artifacts to Azure Blob
    │
    ├── ingestion/                  ← Document ingestion pipeline (pre-pipeline)
    │   ├── parsers/
    │   │   ├── structure_extractor.py   ← Dispatches DOCX/PDF parser
    │   │   ├── docx_parser.py           ← python-docx → DocumentTree
    │   │   └── pdf_parser.py            ← pdfplumber → DocumentTree
    │   ├── chunking/
    │   │   ├── chunk_builder.py         ← DocumentTree → CourseChunk list
    │   │   └── models.py                ← DocumentTree, CourseChunk, ChunkMetadata
    │   ├── enrichment/
    │   │   └── metadata_enricher.py     ← LLM enrichment (summary, concepts, difficulty)
    │   ├── embedding/
    │   │   └── embedding_service.py     ← 4-level embedding generation (3072-dim)
    │   └── storage/
    │       ├── azure_search_client.py   ← Uploads chunks to Azure AI Search index
    │       ├── index_schema.py          ← Azure AI Search index field definitions
    │       └── retrieval_service.py     ← Hybrid BM25+vector search queries
    │
    ├── pipeline/
    │   ├── pipeline.py             ← Direct pipeline entry point (Mode 3)
    │   ├── models.py               ← A0Result, A2Output, A2Stats typed models
    │   │
    │   ├── agent/
    │   │   ├── a0_request_synthesizer/   ← Document parsing, TO generation, classification
    │   │   │   ├── orchestrator/synthesizer.py   ← A0 entry point
    │   │   │   ├── step_01_document_parsing/     ← Parse DOCX/PDF, chunk source files
    │   │   │   ├── step_02_classification/       ← Rule family classification via LLM
    │   │   │   ├── step_03_to_processing/        ← Timed Outline extraction/generation
    │   │   │   └── step_04_post_processing/      ← Metrics enrichment, images
    │   │   │
    │   │   ├── a1_outline_interpreter/   ← LangGraph StateGraph, course_spec builder
    │   │   │   ├── orchestrator/graph.py         ← A1 LangGraph entry point
    │   │   │   └── shared/models/state.py        ← A1State TypedDict
    │   │   │
    │   │   ├── s1_validator/             ← Quality gate on A0+A1 output
    │   │   │
    │   │   ├── section_mapper/           ← Maps TO lessons → vector-retrieved chunks
    │   │   │   ├── orchestrator/runner.py
    │   │   │   └── step_01_map_sections/utils/
    │   │   │       ├── mapper.py         ← Core mapping logic (vector-only)
    │   │   │       ├── section_helpers.py   ← Format detection, IE cleanup
    │   │   │       └── vector_retriever.py  ← Azure AI Search retrieval layer
    │   │   │
    │   │   ├── kc_planner/               ← Knowledge Check placement (3 scenarios)
    │   │   ├── a2_content_generator/     ← LLM content generation + DOCX rendering
    │   │   └── s2_validator/             ← Quality gate on generated content
    │   │
    │   ├── rule_pack_config/
    │   │   ├── packs/
    │   │   │   ├── insurance_ce.py
    │   │   │   ├── iarce.py
    │   │   │   └── firm_element.py
    │   │   └── rule_packs.py            ← resolve_rule_pack(family, difficulty)
    │   │
    │   └── shared_llm_config/
    │       ├── model_registry.py        ← Per-agent deployment registry
    │       ├── model_overrides.json     ← Runtime overrides (settings API)
    │       └── tracer.py                ← Langfuse tracing integration
    │
    ├── models/
    │   ├── db_models.py                 ← SQLAlchemy ORM (jobs, stage_progress, retry_history)
    │   └── job_enums.py                 ← JobStatus, StageStatus, ValidationOutcome
    │
    ├── repositories/
    │   ├── job_repository.py            ← SQL CRUD for jobs and stage progress
    │   └── blob_repository.py           ← Azure Blob Storage wrapper
    │
    └── tests/
        ├── unit/                        ← Unit tests (no running server required)
        └── integration/                 ← Smoke tests (requires API + worker)
```

---

## Architecture Overview

The system has three architectural layers that communicate through a shared state contract rather than direct imports:

```
┌─────────────────────────────────────────────────────────────────┐
│                        Frontend (React 19)                       │
│          Upload → Generate TO → Pipeline Monitor → Editor       │
└────────────────────────────┬────────────────────────────────────┘
                             │ REST / SSE
┌────────────────────────────▼────────────────────────────────────┐
│                     FastAPI Backend                              │
│   main.py (prod + auth)  │  dev_app.py (local, no auth)        │
│                           │                                      │
│  /documents/upload ───────┤──► Ingestion Pipeline               │
│  /documents/generate-to ──┤──► A0 → S1 (to_only) → A1 → S1 (a1_only)  [async job]
│  /jobs ───────────────────┤──► Service Bus (prod) / in-memory   │
│  /jobs/{id}/events ───────┤──► SSE stream                       │
└───────┬──────────────────┬┴────────────────────────────────────┘
        │ Service Bus       │ Background task
        ▼                   ▼
┌──────────────┐   ┌────────────────────────────────────────────┐
│   worker.py  │   │          Ingestion Pipeline                 │
│  (listener)  │   │  Parse → Chunk → Enrich → Embed → Index    │
└──────┬───────┘   └──────────────────┬─────────────────────────┘
       │                              │
       ▼                              ▼
┌──────────────────────────────────────────────────────────────┐
│                   Course Gen Pipeline                         │
│                                                              │
│  /jobs (local dev)   : A0(context prep) → A1 → Section Mapper → KC Planner → A2 → [S2 x3]
│  pipeline.py (direct): A0 → A1 → [S1 gate x3] → Section Mapper → KC Planner → A2 → [S2 x3] │
│                                        │                     │
│                                        │ vector search       │
│                                        ▼                     │
│                                  Azure AI Search             │
│                                        │                     │
│                                        ▼                     │
│                              KC Planner ──► A2 ──► [S2 x3] │
│                                                      │       │
│                                                      ▼       │
│                                             study_guide.docx │
└──────────────────────────────────────────────────────────────┘
```

### Data Flow Between Components

Agents communicate exclusively through `shared_state.json`. No agent imports another agent's code. The orchestrator reads and writes this file between stages.

```
shared_state.json
├── run_id, status
├── extracted_inputs          ← A0: title, LOs, content_sample, indexed_content
├── source_chunks             ← A0: BM25 chunks for A2 fallback retrieval
├── images                    ← A0: extracted images with paragraph indices
├── llm_classification        ← A0: rule_family, confidence
├── llm_to_outline_classification  ← A0: structured Timed Outline JSON
├── agent_outputs
│   ├── A1.course_spec        ← A1: full section hierarchy with para indices
│   ├── section_map           ← Section Mapper: enriched_sections with matched_chunks
│   ├── kc_planner            ← KC Planner: scenario + per-subtopic decisions
│   └── A2                    ← A2: generated sections, stats, docx path
├── s1_validation             ← S1: canonical/latest validation report (includes `phase`)
└── s2_validation             ← S2: ValidationIssue[] with severity + remediation
```

---

## Working Architecture Flow

This section shows the complete, concrete data pipeline from the moment a user uploads a file to the moment a formatted Word document is produced. It traces actual data shapes through each component so you can follow a single document end-to-end.

```
USER
 │
 │  POST /documents/upload  (multipart DOCX/PDF)
 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  API Layer  (dev_app.py / main.py)                                          │
│                                                                             │
│  1. Save file bytes → Azure Blob Storage (prod) / local disk (dev)          │
│  2. Generate document_id = uuid4()                                          │
│  3. IngestionStatusStore.set_status(document_id, "pending")                 │
│  4. BackgroundTasks.add_task(_run_ingestion_background, bytes, name, id)    │
│                                                                             │
│  Response: { blobPath, documentId }                                         │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │  background thread
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  INGESTION PIPELINE  (IngestionOrchestrator)                                │
│                                                                             │
│  set_status(document_id, "processing")                                      │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Step 1 — Parse  (DocumentStructureExtractor)                        │  │
│  │                                                                      │  │
│  │  .docx → DOCXParser → flat_nodes: [DocumentNode]                    │  │
│  │  .pdf  → PDFParser  →   (block_type, level, text, page_num)         │  │
│  │                          ↓                                           │  │
│  │  _build_hierarchy() → DocumentTree                                   │  │
│  │    sections: [DocumentSection(section_id, title, level,              │  │
│  │                               para_start, para_end, nodes)]          │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                               │                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Step 2 — Chunk  (CourseChunkBuilder)                                │  │
│  │                                                                      │  │
│  │  For each DocumentSection:                                           │  │
│  │    • body text → token count (tiktoken cl100k_base)                  │  │
│  │    • if tokens ≤ 600 → one chunk                                     │  │
│  │    • if tokens > 600 → split at paragraph boundaries                 │  │
│  │    • discard chunks < 80 tokens                                      │  │
│  │                                                                      │  │
│  │  Output: CourseChunk {                                               │  │
│  │    chunk_id:   "chunk_{doc_id}_{section_id}_{seq:03d}"              │  │
│  │    raw_text:   "verbatim paragraph text..."                          │  │
│  │    token_count, estimated_read_min, page_num, source_file            │  │
│  │    metadata:   ChunkMetadata() — empty until enrichment              │  │
│  │  }                                                                   │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                               │                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Step 3 — Enrich  (MetadataEnricher) [optional]                     │  │
│  │                                                                      │  │
│  │  LLM call per chunk → fills ChunkMetadata:                          │  │
│  │    summary, keywords, learning_concepts, learning_outcomes,          │  │
│  │    prerequisites, entities, difficulty                               │  │
│  │                                                                      │  │
│  │  Skipped when INGESTION_LLM_DEPLOYMENT is not set.                  │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                               │                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Step 4 — Embed  (MultiLevelEmbeddingService)                       │  │
│  │                                                                      │  │
│  │  For each chunk, 4 separate embedding API calls:                    │  │
│  │    embedding_title    ← chunk.title                                  │  │
│  │    embedding_summary  ← chunk.metadata.summary                      │  │
│  │    embedding_content  ← chunk.raw_text                              │  │
│  │    embedding_keywords ← ", ".join(chunk.metadata.keywords)          │  │
│  │                                                                      │  │
│  │  Model: text-embedding-3-large  →  3072-float vector each           │  │
│  │  On connection error: sets _endpoint_reachable=False, skips rest    │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                               │                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Step 5 — Index  (AzureSearchIngestionClient)                       │  │
│  │                                                                      │  │
│  │  ensure_index_exists()  →  creates "course-chunks" index if missing  │  │
│  │  upload_chunks(chunks)  →  REST POST to Azure AI Search             │  │
│  │                                                                      │  │
│  │  Each document in the upload batch:                                  │  │
│  │  {                                                                   │  │
│  │    "@search.action": "mergeOrUpload",                               │  │
│  │    "chunk_id":       "chunk_abc_sec001_000",                        │  │
│  │    "document_id":    "abc-uuid",                                     │  │
│  │    "raw_text":       "...",                                          │  │
│  │    "embedding_content": [0.012, -0.034, ..., 3072 floats],         │  │
│  │    ...all other fields                                               │  │
│  │  }                                                                   │  │
│  │                                                                      │  │
│  │  Result: succeeded=N  →  set_status(doc_id, "indexed", N)           │  │
│  │  Embed skipped:        →  set_status(doc_id, "parsed")              │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘

  Meanwhile, frontend polls GET /documents/{documentId}/ingestion-status
  every 3s.  "Next" button stays disabled until status ∈ {indexed, parsed}.

USER (continues) — **Generate TO** (`POST /documents/generate-to`)
 │
 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  A0 — Request Synthesizer                                                   │
│  Parse sources → classify rule family → extract/generate Timed Outline       │
│  Output: llm_to_outline.json + shared_state.llm_to_outline_classification   │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  S1 Phase 1 — `phase="to_only"`                                             │
│  A0 checks + AI semantic TO validation                                       │
│  BLOCKER → retry S1 phase 1 only (cached A0 output reused; no doc re-read)   │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  A1 — final TO preparation (course_spec for three-panel review)             │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  S1 Phase 2 — `phase="a1_only"`                                             │
│  A1 sections, word counts, KC, LO coverage, credit hours, assessment rules   │
│  BLOCKER → TOValidationBlockedError (run stops)                             │
│  PASS    → `{ to, rules, s1Validation }` to frontend                          │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │  user reviews TO in three-panel editor
                               │  POST /jobs  (studyGuide, timedOutline, ...)
                               ▼

USER (continues) — **Local course job** (`POST /jobs` in `local_jobs.py`)
 │
 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  A0 — Request Synthesizer  (orchestrator/synthesizer.py)                    │
│                                                                             │
│  step_01_document_parsing:                                                  │
│    Parse all source files → BM25 source_chunks stored in shared_state      │
│    Extract images with paragraph indices                                    │
│                                                                             │
│  step_02_classification:                                                    │
│    LLM (o3) → { family: "insurance_ce", confidence: 0.97 }                 │
│                                                                             │
│  step_03_to_processing:                                                     │
│    If TO doc provided → LLM extracts structured outline from DOCX           │
│    If no TO → LLM generates outline from heading/TOC structure              │
│                                                                             │
│  step_04_post_processing:                                                   │
│    Enrich sections with NAIC metrics (word_count, minutes, credit_hour)    │
│                                                                             │
│  Output: llm_to_outline.json  {                                             │
│    sections: [{ title, subtopics, word_count, minutes,                      │
│                 interactive_elements, credit_hour }]                        │
│    totals: { total_words, total_minutes, total_credit_hours }               │
│  }                                                                          │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │  user reviews TO in three-panel editor
                               │  POST /jobs  (studyGuide, timedOutline, ...)
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  A1 — Outline Interpreter  (LangGraph StateGraph, orchestrator/graph.py)    │
│                                                                             │
│  8-node graph with typed A1State:                                           │
│                                                                             │
│  load_state → parse_document → validate → map_images                       │
│       → enrich → build_spec → detect_issues → persist                      │
│                                                                             │
│  Output: course_spec.json  {                                                │
│    sections: [{                                                             │
│      heading:             "Long-Term Care Insurance Basics",                │
│      level:               1,                                                │
│      para_start:          12,   ← paragraph index in source DOCX           │
│      para_end:            47,                                               │
│      has_knowledge_check: true,                                             │
│      maps_to_objectives:  [0, 1, 2],                                       │
│      learning_objectives: ["Understand...", "Identify..."],                 │
│      images:              [{ para_idx: 18, filename: "img_001.png" }],     │
│      subtopics:           [ ...nested sections... ]                         │
│    }]                                                                       │
│  }                                                                          │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
┌─────────────────────────────────────────────────────────────────────────────┐
│  SECTION MAPPER  (orchestrator/runner.py)                                   │
│                                                                             │
│  Reads: course_spec  +  llm_to_outline                                      │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  _build_spec_meta(spec_sections, n_lessons)                          │  │
│  │                                                                      │  │
│  │  Proportional index distribution — no text matching:                 │  │
│  │  lesson 0 → spec[0..N/M]    → has_kc=T, objectives=[0,1], images=[] │  │
│  │  lesson 1 → spec[N/M..2N/M] → has_kc=F, objectives=[2,3], images=[] │  │
│  │  ...                                                                  │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  For each TO lesson:                                                        │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Build subtopics from TO outline                                     │  │
│  │                                                                      │  │
│  │  Format 1 (breakdown — subtopics are objects):                       │  │
│  │    { title, word_count, minutes, credit_hour, interactive_elements } │  │
│  │                                                                      │  │
│  │  Format 2 (flat — subtopics are strings):                            │  │
│  │    { title }   ← KC-titled strings excluded                          │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Vector Retrieval  (vector_retriever.py)                             │  │
│  │                                                                      │  │
│  │  query = "Long-Term Care Basics. Policy Types. Benefit Triggers"     │  │
│  │            └─ lesson title ──┘  └─ subtopics ─────────────────┘     │  │
│  │                    │                                                  │  │
│  │                    ▼                                                  │  │
│  │  embed_query(query) → [0.021, -0.008, ..., 3072 floats]             │  │
│  │                    │                                                  │  │
│  │                    ▼                                                  │  │
│  │  Azure AI Search REST POST /indexes/course-chunks/docs/search        │  │
│  │  {                                                                   │  │
│  │    "search": "Long-Term Care Basics. Policy Types...",               │  │
│  │    "queryType": "simple",                                            │  │
│  │    "vectorQueries": [                                                │  │
│  │      { "kind":"vector", "vector":[...], "fields":"embedding_content","k":15 }, │
│  │      { "kind":"vector", "vector":[...], "fields":"embedding_summary","k":15 }  │
│  │    ],                                                                │  │
│  │    "top": 15, "select": "chunk_id,raw_text,title,page_num,..."      │  │
│  │  }                                                                   │  │
│  │                    │                                                  │  │
│  │                    ▼                                                  │  │
│  │  Azure returns results sorted by RRF-fused score:                   │  │
│  │  [                                                                   │  │
│  │    { "@search.score": 0.84, "chunk_id":"chunk_abc_sec003_001",      │  │
│  │      "raw_text":"Long-term care insurance provides...",              │  │
│  │      "title":"LTC Basics", "page_num": 4 },                         │  │
│  │    { "@search.score": 0.71, "chunk_id":"chunk_abc_sec003_002", ... },│  │
│  │    ...                                                               │  │
│  │  ]                                                                   │  │
│  │                    │                                                  │  │
│  │                    ▼  filter: score < 0.30 → discard                 │  │
│  │                    │                                                  │  │
│  │  distribute_to_subtopics():                                          │  │
│  │    For each subtopic, score all lesson chunks by keyword overlap     │  │
│  │    with subtopic title.  Top-5 per subtopic.                         │  │
│  │    Re-sort by (page_num, sequence) → document reading order.        │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  Output: enriched_sections.json  {                                          │
│    sections: [{                                                             │
│      title:               "Long-Term Care Insurance",                       │
│      word_count:          "4115",                                           │
│      has_knowledge_check: true,                                             │
│      subtopics: [{                                                          │
│        title:              "Policy Types",                                  │
│        maps_to_objectives: [0, 1],                                          │
│        has_knowledge_check: false,                                          │
│        para_start:          0,   ← 0 = built from TO (no para range)       │
│        para_end:            0,                                              │
│        matched_chunks: [                                                    │
│          { raw_text: "There are three main policy types...",                │
│            similarity_score: 0.84,                                          │
│            source_metadata: { chunk_id, source_file, page_num: 4 } },     │
│          { raw_text: "Indemnity policies pay...", similarity_score: 0.76 },│
│          ...up to 5 chunks in document order                                │
│        ]                                                                    │
│      }]                                                                     │
│    }]                                                                       │
│  }                                                                          │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  KC PLANNER  (auto-selects Scenario A / B / C)                              │
│                                                                             │
│  Mutates enriched_sections in place:                                        │
│    subtopic.has_knowledge_check = true/false  (per scenario logic)          │
│                                                                             │
│  Output: kc_plan.json { scenario: "A", decisions: [...] }                  │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  A2 — Content Generator  (orchestrator/generator.py)                        │
│                                                                             │
│  For each TO lesson → one LLM call (all subtopics batched):                 │
│                                                                             │
│  Source text assembly per subtopic (priority order):                        │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  1. matched_chunks  (primary — vector-retrieved)                       │ │
│  │     merge_to_raw_text([chunk1, chunk2, ...]) →                         │ │
│  │     "There are three main policy types...\n\nIndemnity policies pay..." │ │
│  │                                                                        │ │
│  │  2. BM25 multi-file chunks  (secondary — when source_chunks in state)  │ │
│  │     build_context_for_topic(query, source_chunks, top_k=6) →           │ │
│  │     "--- Additional source material ---\n..."                          │ │
│  │                                                                        │ │
│  │  3. Paragraph range  (tertiary — only when para_start != 0)            │ │
│  │     extract_full_section_text(doc, para_start, para_end) →             │ │
│  │     "--- Supplementary paragraph range ---\n..."                       │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  Prompt → LLM (gpt-5.4-mini) → JSON array [{                               │
│    heading, body_paragraphs: [{type, content}],                             │
│    knowledge_check: { question, choices, correct_index, explanation }       │
│  }]                                                                         │
│                                                                             │
│  Also generates course description + conclusion via separate LLM calls.    │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  S2 — Validator  (quality gate, up to 3 cycles)                             │
│                                                                             │
│  Checks word counts, compliance elements, structural rules.                 │
│  BLOCKER → re-run A2 with validation feedback injected into prompt          │
│  PASS    → render_study_guide_from_state()                                  │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  DOCX RENDERER  (step_04_render_docx/utils/doc_formatter.py)                │
│                                                                             │
│  build_study_guide_docx(course_title, sections, learning_objectives, ...)   │
│    → styled Word document with headings, callouts, KC boxes, images         │
│    → study_guide.docx                                                       │
│                                                                             │
│  Uploaded to Azure Blob Storage → job marked COMPLETED                      │
└─────────────────────────────────────────────────────────────────────────────┘

USER
 │
 │  GET /jobs/{jobId}/artifacts/download
 ▼
 study_guide.docx  (ready for Lectora publishing)
```

---

## How Azure AI Search Works

Azure AI Search is a fully managed cloud search service that combines traditional BM25 full-text search with HNSW approximate nearest-neighbour vector search in a single query. This system uses it as the primary content store and retrieval engine for all source material.

### The Index

The index is a structured store of documents (called "chunks" here), each with searchable text fields and high-dimensional vector fields. The index is created once on first ingestion via `index_schema.py` and reused across all subsequent uploads.

```
Index: "course-chunks"
│
├── Text fields (BM25 searchable)
│   ├── title          — chunk heading, en.microsoft analyser
│   ├── raw_text       — full verbatim chunk text, always retrievable
│   ├── summary        — LLM-generated summary
│   ├── keywords       — comma-separated term list
│   └── searchable_text — catch-all BM25 field (not retrievable)
│
├── Filterable fields
│   ├── document_id    — filter to a specific uploaded file
│   ├── section_id     — filter to a document section
│   ├── source_file    — original filename
│   ├── difficulty     — "introductory" / "intermediate" / "advanced"
│   └── page_num       — for sorting by document position
│
└── Vector fields (HNSW, cosine similarity, 3072 dimensions)
    ├── embedding_title    — title vector
    ├── embedding_summary  — summary vector
    ├── embedding_content  — raw_text vector  ← primary for content retrieval
    └── embedding_keywords — keywords vector  ← primary for assessment
```

### HNSW Index (Hierarchical Navigable Small World)

Every vector field is backed by an HNSW graph. The graph is built during document upload and allows approximate nearest-neighbour (ANN) search in sub-millisecond time at scale, without scanning every vector in the index.

HNSW parameters used in this system:

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `metric` | `cosine` | Distance = 1 − (A·B / ‖A‖‖B‖). Chunks with similar meaning score near 1. |
| `m` | `4` | Each node maintains 4 bidirectional links per layer. Lower = smaller index; higher = better recall. |
| `efConstruction` | `400` | Graph quality during build. Higher = better index quality, slower build. |
| `efSearch` | `500` | Beam width during query. Higher = better recall, slower search. |

### How a Query Executes

When `retrieve_topic("Long-Term Care Insurance. Policy Types.")` is called:

**Step 1 — Embed the query**
```
embed_query(text) → AsyncAzureOpenAI.embeddings.create(
    model="text-embedding-3-large",
    input=[text],
    dimensions=3072
) → [0.021, -0.008, 0.043, ..., 3072 floats]
```
The resulting vector represents the semantic meaning of the query in the same embedding space as the indexed chunks.

**Step 2 — Send the hybrid search request**
```http
POST https://course-embeddings.search.windows.net
     /indexes/course-chunks/docs/search?api-version=2024-07-01

{
  "search": "Long-Term Care Insurance. Policy Types.",
  "queryType": "simple",
  "vectorQueries": [
    {
      "kind":   "vector",
      "vector": [0.021, -0.008, ...],
      "fields": "embedding_content",
      "k":      15
    },
    {
      "kind":   "vector",
      "vector": [0.021, -0.008, ...],
      "fields": "embedding_summary",
      "k":      15
    }
  ],
  "top":    15,
  "select": "chunk_id,document_id,section_id,source_file,page_num,
             title,parent_title,raw_text,summary,keywords,
             difficulty,token_count,estimated_read_min"
}
```

**Step 3 — Parallel retrieval paths**

Azure AI Search runs three sub-queries simultaneously:

```
Query text  ──────────────────────────────────────────────►  BM25 ranker
                                                              (inverted index,
                                                               TF-IDF scoring)

Query vector  ──►  HNSW graph on embedding_content  ──────►  Vector ranker 1
                                                              (cosine similarity)

Query vector  ──►  HNSW graph on embedding_summary  ───────►  Vector ranker 2
                                                              (cosine similarity)
```

Each path independently ranks all chunks and returns its own ordered result list.

**Step 4 — Reciprocal Rank Fusion (RRF)**

Azure merges the three result lists using RRF. For each chunk, its score across all lists is combined as:

```
RRF_score(chunk) = Σ  1 / (k + rank_in_list)
                   lists

where k = 60  (a constant that dampens the effect of very high ranks)
```

A chunk that appears highly in all three lists (BM25 + both vector paths) gets a much higher final score than one that appears in only one list. RRF does not require score normalisation across dissimilar scoring functions, which is why it works cleanly for BM25+vector fusion.

**Step 5 — Score filtering and return**

```python
# retrieval_service.py
results = resp.json()["value"]   # sorted by @search.score descending

# vector_retriever.py
chunks = [
    VectorChunk(raw_text=r["raw_text"], similarity_score=r["@search.score"], ...)
    for r in results
    if r.get("raw_text") and float(r["@search.score"]) >= 0.30
]
```

Chunks scoring below `0.30` are discarded as semantically irrelevant. The threshold was chosen to eliminate near-random matches while retaining moderately relevant content.

### Cosine Similarity Explained

Cosine similarity measures the angle between two vectors, not their magnitude. Two chunks are "similar" when their embedding vectors point in the same direction in 3072-dimensional space — regardless of how long or short the original text was.

```
similarity = (A · B) / (‖A‖ × ‖B‖)

Score 1.00 → identical direction (same meaning)
Score 0.90 → strongly related topic
Score 0.70 → related but different angle
Score 0.50 → loosely connected
Score 0.30 → threshold — below this = discard
Score 0.00 → perpendicular (unrelated)
Score < 0  → opposite meaning (rare in practice)
```

Because `text-embedding-3-large` was trained on massive text corpora, chunks about "policy surrender charges" and "surrender fees for annuities" will have high cosine similarity even though they share no words. This is what makes vector search superior to keyword matching for compliance course material, where terminology varies across regulatory bodies and course authors.

### Hybrid Search Advantage

Running BM25 and vector search in parallel, then fusing with RRF, captures the strengths of both:

| Scenario | BM25 | Vector |
|----------|------|--------|
| Query contains exact phrase from chunk | Excellent | Good |
| Query uses synonym or paraphrase | Poor | Excellent |
| Query contains regulatory acronym (HIPAA, ERISA) | Good | Moderate |
| Query and chunk share a topic but no keywords | Poor | Excellent |
| Short query, long chunk | Moderate | Good |

In practice, hybrid search returns 15–25% more relevant results than either approach alone for the compliance education domain.

### Document Filtering

When a `document_id` is passed to `retrieve_topic()`, an OData filter is applied server-side:

```http
"filter": "document_id eq 'abc-1234-uuid'"
```

This restricts the search to chunks from a specific uploaded document, which matters when multiple course authors have indexed different materials in the same index. In the current pipeline, filtering is optional; searches run across the full index when `document_id` is `None`.

### Re-ranking for Subtopics

After Azure returns the lesson-level results, `distribute_to_subtopics()` performs a second local re-ranking without additional API calls:

```python
def _subtopic_relevance(chunk, subtopic_title):
    sub_tokens  = _keyword_tokens(subtopic_title)   # e.g. {"policy", "types", "indemnity"}
    chunk_tokens = _keyword_tokens(chunk.raw_text[:1000])
    overlap      = len(sub_tokens & chunk_tokens)    # how many tokens match
    keyword_boost = min(0.30, overlap * 0.06)        # up to +0.30 boost
    return min(1.0, chunk.similarity_score + keyword_boost)
```

This shifts chunks that explicitly discuss the subtopic topic to the front of that subtopic's result list, without re-embedding or re-querying. The top-5 scoring chunks are then re-sorted by `(page_num, sequence_number)` to restore document reading order before being attached as `matched_chunks`.

---

## Architecture Flow (Step by Step)

### Step 1 — User Uploads Source Files

**Endpoint:** `POST /documents/upload`

The user uploads one or more `.docx` or `.pdf` files through the frontend. Each file is:

1. Saved to Azure Blob Storage (production) or local disk (dev mode)
2. Assigned a unique `document_id`
3. Queued for background ingestion via `BackgroundTasks`
4. Tracked in the in-memory `IngestionStatusStore` with a 4-hour TTL

Status transitions: `pending` → `processing` → `indexed` (or `parsed` / `failed`)

The frontend polls `GET /documents/{document_id}/ingestion-status` every 3 seconds. The **Next** button in the onboarding wizard remains disabled until status reaches a terminal state (`indexed` or `parsed`), ensuring embeddings exist in Azure AI Search before the pipeline runs.

---

### Step 2 — Content Extraction and Normalisation

**Module:** `lectora_backend/ingestion/parsers/`

The `DocumentStructureExtractor` dispatches to the appropriate parser based on file extension:

- **DOCX** → `DOCXParser` (python-docx): reads paragraphs, tables, and headings; assigns paragraph indices used for image mapping
- **PDF** → `PDFParser` (pdfplumber): extracts text blocks with page numbers

Both parsers produce a `DocumentTree`:

```
DocumentTree
├── document_id, filename, file_type, total_pages
├── sections: [DocumentSection]          ← hierarchical structure
│   ├── section_id, title, level
│   ├── para_start, para_end             ← paragraph index range in source
│   └── nodes: [DocumentNode]            ← individual text blocks
└── flat_nodes: [DocumentNode]           ← all nodes in document order
```

Each `DocumentNode` carries its block type (`HEADING`, `PARAGRAPH`, `LIST_ITEM`, `TABLE`, `CAPTION`), heading level, text content, and optional page number.

---

### Step 3 — Chunking

**Module:** `lectora_backend/ingestion/chunking/chunk_builder.py`

`CourseChunkBuilder.build()` walks the `DocumentTree` section by section and splits content at paragraph boundaries to produce `CourseChunk` objects.

**Chunk sizing:**
- Target: 600 tokens max (`MAX_TOKENS`)
- Minimum: 80 tokens (`MIN_TOKENS`) — smaller fragments are discarded
- Token counting: tiktoken `cl100k_base` encoder; falls back to `words × 1.33` approximation when tiktoken is unavailable

**Chunk ID format:** `chunk_{document_id}_{section_id}_{seq:03d}`

Each `CourseChunk` carries:
- `raw_text` — the full verbatim chunk text (always stored, always retrievable)
- `chunk_id`, `document_id`, `section_id`, `source_file`, `page_num`
- `token_count`, `estimated_read_min`
- `searchable_text` — BM25-searchable concatenation of title + filename + raw_text prefix
- Four `embedding_*` vector fields (populated in Step 4)

---

### Step 4 — Embedding Generation

**Module:** `lectora_backend/ingestion/embedding/embedding_service.py`

`MultiLevelEmbeddingService` generates **four independent 3072-dimensional vectors** per chunk using the `text-embedding-3-large` model on Azure OpenAI:

| Vector field        | Input to embedding model          | Search use                     |
|---------------------|-----------------------------------|--------------------------------|
| `embedding_title`   | chunk title                       | Structural / outline search    |
| `embedding_summary` | LLM-generated summary             | High-level topic matching      |
| `embedding_content` | full `raw_text`                   | Semantic content retrieval     |
| `embedding_keywords`| comma-joined keywords             | Assessment / KC generation     |

Embeddings are generated in batches with per-batch error handling. If the embedding endpoint is unreachable, the service sets an `_endpoint_reachable = False` flag on first failure and skips all remaining batches for that ingestion run, preventing repeated timeout delays. The chunk is still uploaded to the index with `raw_text` intact (BM25 search still works; vector search degrades gracefully).

**Optional LLM enrichment** (`MetadataEnricher`) runs before embedding and populates `ChunkMetadata`:
- `summary`, `keywords`, `learning_concepts`, `learning_outcomes`, `prerequisites`, `difficulty`

Enrichment is skipped when `INGESTION_LLM_DEPLOYMENT` is not set, without blocking the pipeline.

---

### Step 5 — Storage in Azure AI Search

**Module:** `lectora_backend/ingestion/storage/`

`AzureSearchIngestionClient` ensures the `course-chunks` index exists (creating it on first run via `index_schema.py`) then uploads chunks in batches using the Azure AI Search REST API (`2024-07-01`).

**Index schema highlights:**
- `chunk_id` (key), `document_id`, `section_id`, `source_file`, `page_num` — identity and provenance
- `title`, `parent_title`, `raw_text`, `summary` — BM25-searchable text fields (en.microsoft analyser)
- `keywords`, `skills`, `learning_concepts`, `learning_outcomes`, `entities` — filterable/facetable collections
- `difficulty` — filterable string for assessment generation
- `embedding_title/summary/content/keywords` — HNSW vector fields (cosine metric, 3072 dims, m=4, efConstruction=400)
- `searchable_text` — BM25-only catch-all field (not retrievable)

After successful upload, `IngestionStatusStore` is updated to `status: "indexed"`.  
If embedding generation was skipped but chunks were uploaded, status is `"parsed"` (BM25 still works).

---

### Step 6 — Section Mapping via Similarity Search

**Modules:** `pipeline/agent/section_mapper/`

The Section Mapper is the bridge between the Timed Outline structure and the indexed source content. It runs after A1 produces `course_spec` and S1 validates both.

**Architecture (three layers):**

```
Retrieval Layer   → vector_retriever.py
Mapping Layer     → mapper.py
Generation Layer  → A2 content generator (downstream consumer)
```

**Retrieval layer (`vector_retriever.py`)**

`CourseRetrievalService.retrieve_topic()` executes a **hybrid BM25 + vector search** query against the Azure AI Search index:

1. The query text is embedded using the same `text-embedding-3-large` model
2. Two parallel vector queries run against `embedding_content` and `embedding_summary`
3. Results are fused by Azure AI Search's Reciprocal Rank Fusion (RRF) algorithm
4. Chunks with `@search.score < 0.30` are discarded
5. Remaining chunks are returned sorted by score descending

**Mapping layer (`mapper.py`)**

For each TO lesson:

1. **Metadata propagation** — KC flags, objective indices, and images from A1's `course_spec` are distributed to TO lessons by proportional index (no text matching)
2. **Subtopic building** — subtopics are constructed directly from the TO outline:
   - Format 1 (breakdown): TO subtopics are timing objects → `_build_breakdown_subtopics()`
   - Format 2 (flat): TO subtopics are strings → `_build_flat_subtopics()`
3. **Vector retrieval** — one search call per lesson (not per subtopic) using a combined query: `lesson_title. subtopic_title_1. subtopic_title_2. ...`
4. **Chunk distribution** — `distribute_to_subtopics()` scores each lesson chunk against each subtopic title via keyword overlap, selects top-5 per subtopic, and re-sorts by document order (page_num + sequence number)
5. **`matched_chunks` attached** — each subtopic dict gains `matched_chunks: [{raw_text, similarity_score, source_metadata}]`

The output `enriched_sections.json` is written to the shared state directory and stored in `shared_state["agent_outputs"]["section_map"]`.

---

### Step 7 — Knowledge Check Planning

**Module:** `pipeline/agent/kc_planner/`

The KC Planner runs after Section Mapper and mutates `has_knowledge_check` flags on subtopics in `enriched_sections`. It auto-selects one of three scenarios:

**Scenario A — Source document has KCs (A1 flagged them)**
Cross-references raw-doc KC positions with the TO's `interactive_elements`. Keeps `has_knowledge_check=True` only where the TO confirms KC placement. Decisions: `confirmed_by_to` or `removed_not_in_to`.

**Scenario B — No raw-doc KCs, TO is available**
Derives KC placement from the TO `interactive_elements` or KC-titled subtopics. Marks the last substantive subtopic of each qualifying lesson. Decision: `kc_from_to`.

**Scenario C — No KCs anywhere, no TO**
Applies the active rule pack's `kc_placement_rules` algorithmically: respects cadence intervals, forbidden placement types (intro, summary, conclusion), and min/max KC counts per lesson. Decision: `kc_from_rule_pack`.

Output: `kc_plan.json` with `{scenario, decisions[]}` stored in shared state.

---

### Step 8 — Course Content Generation

**Module:** `pipeline/agent/a2_content_generator/`

A2 generates study-guide content one lesson at a time. For each lesson, all subtopics are sent in a single LLM call, which returns a JSON array of structured content sections.

**Source text assembly per subtopic (priority order):**

1. **`matched_chunks`** (primary) — vector-retrieved chunks from the Section Mapper, merged into a coherent block using `merge_to_raw_text()`. Adjacent duplicate paragraphs are deduplicated.
2. **BM25 multi-file chunks** (secondary) — when `source_chunks` from A0 are available, `build_context_for_topic()` retrieves topic-relevant passages via keyword overlap and appends them.
3. **Paragraph range extraction** (tertiary) — `extract_full_section_text(para_start, para_end)` from the original DOCX, appended only when the subtopic has a non-zero para range.

The assembled source text is trimmed to 3× the subtopic's word-count target before being sent to the LLM.

**Word count distribution:**
- Format 1 (breakdown): each subtopic carries its own TO word-count target
- Format 2 (flat): the lesson word-count is distributed proportionally based on source-text length across subtopics

**Additional generation:**
- `_build_course_description()` — LLM-generated overview paragraph from content_sample + LOs
- `_build_course_conclusion()` — LLM-generated closing section

**Output:** `generated_content.json` (all sections as structured JSON) + `study_guide.docx` (styled Word document rendered only after S2 passes)

---

### Step 9 — Validation and Output

**Module:** `pipeline/agent/s2_validator/`

S2 validates generated content against rule-pack constraints and word-count targets before assembling the final DOCX.

**Word count validation (two scenarios):**
- Source ≤ 140% of TO target → enforce 50%–80% generation band (blocker outside range)
- Source > 140% of TO target → compare generated content directly to TO target (over = blocker, under = warning)

A section-level deviation check compares each lesson's generated word count to its individual TO target.

**Severity levels:**
| Level | Effect |
|-------|--------|
| `blocker` | Stops pipeline; triggers A2 retry with feedback injected into prompt |
| `critical` | Flagged for review; pipeline continues |
| `warning` | Noted; pipeline continues |
| `info` | Informational only |

After S2 passes (or passes with warnings), `render_study_guide_from_state()` assembles the final styled `study_guide.docx`. The DOCX is written to the job's output directory and uploaded to Azure Blob Storage (production) or left on local disk (dev mode).

---

## Pipeline Agents

| Agent | Entry Point | Role |
|-------|-------------|------|
| **A0** | `orchestrator/synthesizer.py` | Parses source docs, classifies rule family, extracts/generates Timed Outline, chunks all files |
| **A1** | `orchestrator/graph.py` | LangGraph StateGraph (8 nodes); parses document structure, maps images, enriches with LLM, builds `course_spec` |
| **S1** | `main.py` | Quality gate with `phase`: `to_only` (A0+AI, generate-TO), `a1_only` (A1 checks), `full` (main pipeline: A0→A1→S1 after A1) |
| **Section Mapper** | `orchestrator/runner.py` | Maps TO structure to vector-retrieved source content; produces `enriched_sections` |
| **KC Planner** | `main.py` | Determines KC placement via 3 auto-selected scenarios; mutates `has_knowledge_check` flags |
| **A2** | `orchestrator/generator.py` | Generates lesson content via LLM; renders final styled DOCX |
| **S2** | `main.py` | Validates content quality and word counts; blockers trigger A2 retry (max 3 cycles) |

### A0 Internal Steps

```
step_01_document_parsing  → Parse DOCX/PDF → DocumentTree → BM25 chunks
step_02_classification    → LLM rule family classification (o3 model)
step_03_to_processing     → Parse existing TO -OR- generate TO from structured content
step_04_post_processing   → NAIC metrics enrichment, image extraction
```

### A1 LangGraph Nodes

```
load_state → parse_document → validate → map_images → enrich → build_spec → detect_issues → persist
```

### Model Registry (Default Deployments)

| Agent ID | Default Model | Role |
|----------|--------------|------|
| `A0` | `o3` | Rule family classification (reasoning model) |
| `A0_TO` | `gpt-5.4-mini` | Timed-outline extraction |
| `A1` | `gpt-5.4-mini` | Section enrichment via LangGraph |
| `A2` | `gpt-5.4-mini` | Content generation |

Override at runtime via `PUT /settings/models` — changes take effect immediately without restart, persisted to `pipeline/shared_llm_config/model_overrides.json`.

---

## Ingestion Pipeline

The ingestion pipeline runs as a background task when a file is uploaded. It is independent of the course generation pipeline and must complete before a course generation job is submitted.

```
Upload
  │
  ▼
DocumentStructureExtractor
  │  (DOCX → DOCXParser, PDF → PDFParser)
  ▼
DocumentTree (sections + flat_nodes)
  │
  ▼
CourseChunkBuilder
  │  (600 token max, 80 token min, tiktoken cl100k_base)
  ▼
CourseChunk list (with chunk_id, raw_text, page_num, source_file)
  │
  ├──► MetadataEnricher (optional — requires INGESTION_LLM_DEPLOYMENT)
  │      LLM → summary, keywords, learning_concepts, difficulty, etc.
  │
  ├──► MultiLevelEmbeddingService
  │      text-embedding-3-large → 4 × 3072-dim vectors per chunk
  │      (embedding_title, embedding_summary, embedding_content, embedding_keywords)
  │
  └──► AzureSearchIngestionClient
         Creates index if missing → uploads chunks
         Updates IngestionStatusStore → "indexed"
```

---

## Vector Retrieval System

### Index: `course-chunks` (Azure AI Search)

The HNSW vector index stores all ingested chunks with hybrid search capability:

- **BM25 text search** on `title`, `raw_text`, `summary`, `keywords` (en.microsoft analyser)
- **Vector search** on `embedding_content` and `embedding_summary` (cosine similarity, HNSW)
- **Document filtering** by `document_id`, `difficulty`, `section_id`

### Retrieval Strategies (CourseRetrievalService)

| Method | Vector fields | Primary use |
|--------|--------------|-------------|
| `retrieve_topic()` | content + summary | Section Mapper, lesson body generation |
| `retrieve_for_outline()` | title + summary | Module scaffolding, section overview |
| `retrieve_for_objectives()` | content + keywords | KC planner, LO generation |
| `retrieve_for_assessment()` | content + keywords | Quiz / knowledge check generation |

### Section Mapper Retrieval Flow

```
Lesson title + subtopic titles
         │
         ▼
  build_query()           ← combines title, subtitle, objective strings
         │
         ▼
  embed_query()           ← text-embedding-3-large → 3072-dim vector
         │
         ▼
  Azure AI Search         ← hybrid BM25 + dual vector query (RRF fusion)
         │
         ▼
  Score filter            ← discard chunks below 0.30
         │
         ▼
  distribute_to_subtopics()
         │  keyword-overlap re-ranking per subtopic
         │  top-5 chunks per subtopic
         │  re-sort by document order (page_num + sequence)
         ▼
  matched_chunks on each subtopic
```

---

## Azure Services

| Service | Role | Config key |
|---------|------|------------|
| **Azure OpenAI** (main resource) | Chat completions for A0, A1, A2, KC Planner, MetadataEnricher | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` |
| **Azure OpenAI** (embeddings resource, optional) | Dedicated resource for `text-embedding-3-large` | `AZURE_OPENAI_EMBEDDINGS_RESOURCE_NAME`, `AZURE_OPENAI_EMBEDDINGS_KEY` |
| **Azure AI Search** | Chunk storage, hybrid BM25+vector retrieval | `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_API_KEY`, `AZURE_SEARCH_INDEX_NAME` |
| **Azure Blob Storage** | Source documents, shared_state, artifacts, generated courses | `AZURE_STORAGE_CONNECTION_STRING`, `BLOB_CONTAINER_NAME` |
| **Azure Service Bus** | Job queue between API and worker | `SERVICE_BUS_CONNECTION_STRING`, `QUEUE_NAME` |
| **Microsoft Entra ID** | OAuth2 Bearer token auth on production routes | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` |

### Blob Storage Layout

```
course-generation-artifacts/
└── {courseSlug}/{jobId}/
    ├── doc/        ← original input .docx / .pdf files
    ├── output/     ← pipeline artifacts (JSON + final study_guide.docx)
    ├── images/     ← extracted image binaries
    ├── logs/       ← stage logs + pipeline_run_log.json
    └── state/      ← shared_state.json + pipeline_shared_state.json

uploaded-documents/
└── uploads/{document_id}/   ← source files uploaded by users

generated-courses/
└── {courseSlug}/             ← save-to-Azure exports
```

---

## Execution Modes

### Mode 1 — Production API + Worker

Full Azure stack. Authentication via Entra ID. Jobs queued via Service Bus.

```
API (main.py)
  POST /jobs → publish to Service Bus → return 202 + jobId

Worker (worker.py)
  consume message → lock renewal (30 min max) → run pipeline → upload artifacts → mark COMPLETED
```

Retry logic in `core/orchestrator.py`:
- S1 gate: up to 3 full A0→A1→S1 cycles
- S2 gate: up to 3 A2-only cycles with feedback from prior validation

### Mode 2 — Local Dev (`dev_app`)

No Azure Service Bus, no Blob Storage, no auth. Full frontend workflow supported.

```
API (dev_app.py)
  POST /jobs → local_course_job_store (in-memory, 2 hr TTL, max 3 concurrent)
            → runs pipeline in background thread
            → writes artifacts to pipeline/courses/{courseSlug}/{jobId}/
```

Only requires `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY`. Azure AI Search is optional — the pipeline runs without vector retrieval and falls back to BM25 chunked search.

### Mode 3 — Direct Pipeline

No API, no database, no worker. Fastest for development and testing.

```bash
python3 lectora_backend/pipeline/pipeline.py
```

Writes all artifacts to `pipeline/shared_state/`.

---

## API Endpoints

### Document Upload and TO Generation

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/documents/upload` | Upload DOCX, PDF, or JSON timed-outline. Returns `{blobPath, documentId}`. Triggers background ingestion. |
| `GET` | `/documents/{documentId}/ingestion-status` | Poll indexing progress. Returns `{status, total_chunks, error?}`. |
| `POST` | `/documents/generate-to` | Run generate-TO pipeline async: **A0 → S1 (to_only) → A1 → S1 (a1_only)**. Returns `{jobId, pollUrl}`. Pass `wait=true` for sync. |
| `GET` | `/documents/generate-to/jobs/{jobId}` | Poll async job. Returns `{status, to, rules, toBlobPath, s1Validation?}` when complete. Failed jobs may also include `s1Validation` for blocked details. |

### Course Generation Jobs

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/jobs` | Create a course generation job. Returns `202` with `{jobId}`. |
| `GET` | `/jobs/{jobId}` | Job status, stage progress array, artifact list. |
| `GET` | `/jobs/{jobId}/events` | SSE stream of real-time stage updates and logs. |
| `GET` | `/jobs/{jobId}/course` | Course content JSON after completion. |
| `GET` | `/jobs/{jobId}/artifacts/download` | Download final `study_guide.docx`. |
| `POST` | `/jobs/{jobId}/retry` | Retry from a specific stage (production only). |

**`POST /jobs` body:**
```json
{
  "studyGuide":    "blob/path/to/study_guide.docx",
  "timedOutline":  "blob/path/to/timed_outline.docx",
  "courseTitle":   "Long-Term Care Insurance",
  "courseType":    "insurance_ce",
  "requestedBy":   "user@example.com",
  "to_override":   { },
  "source_file_paths": ["blob/path/to/supplemental.pdf"]
}
```

### Settings

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/settings` | Current per-agent model configs + available models. |
| `PUT` | `/settings/models` | Bulk-update agent deployments. Takes effect immediately. |
| `POST` | `/settings/models/reset` | Revert one or all agents to default deployments. |

### Storage

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/storage/browse` | Browse artifacts container. |
| `GET` | `/storage/uploaded-documents/browse` | Browse uploaded source files. |
| `GET` | `/storage/file` | Download or preview any artifact. |
| `POST` | `/storage/delete` | Delete files/folders; cancels in-flight jobs that touch those paths. |

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health/` | Service health check. |

---

## Rule Packs

Three course families are supported. A0 classifies the uploaded source document into one of these families using an LLM call.

| Family | File | Description |
|--------|------|-------------|
| `insurance_ce` | `packs/insurance_ce.py` | NAIC CE insurance licensing courses |
| `iarce` | `packs/iarce.py` | IARCE renewal education |
| `firm_element` | `packs/firm_element.py` | FINRA Firm Element continuing education |

Each rule pack defines:
- `assessment_rules` — KC question count, format, distractors
- `style_constraints` — tone, reading level, prohibited terms
- `compliance_elements` — mandatory disclosures, regulatory citations
- `content_rules` — content depth, example requirements
- `kc_placement_rules` — cadence, forbidden placements, min/max per lesson

Load at runtime: `resolve_rule_pack(rule_family, difficulty)` in `rule_pack_config/rule_packs.py`.

### NAIC CE Credit Hour Metrics

| Constant | Value |
|----------|-------|
| Reading speed | 180 words/minute |
| Credit hour | 50 minutes = 9,000 words |
| Basic difficulty | 1.00× multiplier |
| Intermediate | 1.25× multiplier |
| Advanced | 1.50× multiplier |

NAIC rounding: fractional part ≥ 0.50 rounds up; < 0.50 rounds down.

---

## Knowledge Check Planner

Auto-selects one of three scenarios based on source document analysis:

```
Has KC headings in source doc?
       │
      YES ──► Scenario A: Cross-reference with TO
              ├── TO confirms KC → confirmed_by_to
              └── TO has no KC  → removed_not_in_to
       │
      NO
       │
      Is TO available?
       │
      YES ──► Scenario B: Derive KC from TO
              └── Mark last substantive subtopic per KC lesson → kc_from_to
       │
      NO ───► Scenario C: Algorithmic placement from rule pack
              └── Cadence + forbidden placements + min/max → kc_from_rule_pack
```

Output written to `kc_plan.json` and `shared_state["agent_outputs"]["kc_planner"]`.

---

## Environment Variables

Copy `.env.example` to `.env` and fill all values before running.

```env
# Database — must be absolute path for SQLite
DATABASE_URL=sqlite:////absolute/path/to/lectora.db

# Azure Service Bus (production only)
SERVICE_BUS_NAMESPACE=your-namespace.servicebus.windows.net
SERVICE_BUS_CONNECTION_STRING=Endpoint=sb://...
QUEUE_NAME=course-jobs

# Azure OpenAI (main resource — used for all LLM calls)
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_DEPLOYMENT=gpt-4o        # default model (overridden per-agent)

# Microsoft Entra ID (production auth only)
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
AZURE_CLIENT_SECRET=your-secret
AZURE_AUDIENCE=                        # leave blank for default

# Azure Blob Storage
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;...
BLOB_CONTAINER_NAME=regedlectoraaistorage

# Azure AI Search (vector retrieval)
AZURE_SEARCH_ENDPOINT=https://your-search.search.windows.net
AZURE_SEARCH_API_KEY=your-search-api-key
AZURE_SEARCH_INDEX_NAME=course-chunks

# Embeddings — leave resource name empty to use main OpenAI resource
AZURE_OPENAI_EMBEDDINGS_RESOURCE_NAME=   # optional dedicated resource
AZURE_OPENAI_EMBEDDINGS_KEY=             # optional key for dedicated resource
INGESTION_EMBEDDING_DEPLOYMENT=text-embedding-3-large

# Ingestion LLM (metadata enrichment) — falls back to AZURE_OPENAI_DEPLOYMENT
INGESTION_LLM_DEPLOYMENT=

# Langfuse tracing (optional)
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com

# CORS (comma-separated browser origins)
CORS_ORIGINS=http://localhost:5173,http://localhost:3000,http://localhost:8080
CORS_ORIGIN_REGEX=https://your-app\.netlify\.app
```

### Minimum Required for Local Dev (`dev_app`)

```env
DATABASE_URL=sqlite:////absolute/path/to/lectora.db
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_DEPLOYMENT=gpt-4o
```

Azure AI Search, Service Bus, and Blob Storage are all optional in dev mode.

---

## Local Development Setup

### Prerequisites

- Python 3.11+
- Node.js 20+ (frontend only)

### Backend

```bash
cd lectora-course-gen-engine

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env — at minimum set DATABASE_URL, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY

# Apply database migrations
alembic upgrade head

# Start dev API server (no auth, no Azure Service Bus)
uvicorn lectora_backend.dev_app:app --reload
# API available at http://localhost:8000
# Swagger docs at http://localhost:8000/docs
```

### Production mode (requires all Azure services)

```bash
# Terminal 1 — API server
uvicorn lectora_backend.main:app --reload

# Terminal 2 — Worker (listens on Service Bus)
PYTHONUNBUFFERED=1 python -m lectora_backend.worker
```

### Direct pipeline execution (no API or worker)

```bash
python3 lectora_backend/pipeline/pipeline.py
```

Edit `pipeline.py` to set source file paths. Artifacts are written to `pipeline/shared_state/`.

---

## Docker Deployment

### Dev mode (no Service Bus or Blob Storage)

```bash
cp .env.example .env
# Fill AZURE_OPENAI_* at minimum

docker compose up --build -d
# API: http://localhost:8000/docs
```

The dev compose uses `dev_app.py`, persists SQLite to a named volume, and sets `RUN_MIGRATIONS=0`.

### Production mode

```bash
docker compose -f docker-compose.prod.yml up --build -d
```

The prod compose runs `main.py` + a separate worker container. Sets `RUN_MIGRATIONS=1` so Alembic runs on API startup. Requires all Azure service vars to be set in `.env`.

---

## Database Migrations

The application uses Alembic for schema management. Tables are never auto-created at runtime.

```bash
# Apply all pending migrations
alembic upgrade head

# Show current revision
alembic current

# Show pending migrations
alembic history

# Create a new migration after changing db_models.py
alembic revision --autogenerate -m "add column xyz"
```

**Three tables:**
- `jobs` — job identity, status, blob path reference
- `stage_progress` — per-stage status and validation outcomes
- `retry_history` — retry attempts with trigger reasons

---

## Running Tests

```bash
# Unit tests (no running server required)
python3 -m pytest lectora_backend/tests/unit/ -v

# Single unit test file
python3 -m pytest lectora_backend/tests/unit/test_azure_course_artifacts.py -v

# Integration smoke test (requires API + worker running and all Azure services)
python3 -m pytest lectora_backend/tests/integration/test_job_flow_smoke.py -v
```

---

## Key Design Principles

- **Agents never import each other.** Communication is exclusively through `shared_state.json`.
- **Vector retrieval is the single content retrieval path.** The Section Mapper uses Azure AI Search for all source content mapping. BM25 keyword retrieval from A0 chunks acts only as a secondary fallback in A2 when vector chunks are absent.
- **Ingestion must complete before course generation.** The frontend enforces this via the ingestion status polling flow.
- **Generate-TO uses two S1 passes.** Phase 1 (`to_only`) blockers retry **S1 only** with cached A0 output; Phase 2 (`a1_only`) blockers stop after A1.
- **Direct `pipeline.py` runs S1 `full` and restarts A0→A1→S1 when blocked.** Local `POST /jobs` flow currently skips S1 and proceeds A0(context prep) → A1 → Section Mapper → KC Planner → A2 → S2.
- **`study_guide.docx` is rendered only after S2 passes.** No DOCX is produced for content that fails validation.
- **`DATABASE_URL` must be an absolute path** when using SQLite. Relative paths resolve differently from the API vs worker processes.
- **Azure OpenAI o-series models require `max_completion_tokens`**, not `max_tokens`.
- **Langfuse tracing is optional.** Omitting the keys disables tracing without affecting the pipeline.
