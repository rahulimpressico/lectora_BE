from .base import *

async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(
        ...,
        description="A .docx or .pdf source document, or a .json Timed Outline file.",
    ),
    course_topic: str = Form(
        ...,
        alias="courseTopic",
        description="Course topic / folder name (required). Creates {topic}/ in uploaded-documents.",
    ),
) -> UploadDocumentResponse:
    """
    Save an uploaded DOCX, PDF, or JSON file under ``{course_topic}/{filename}``
    in the uploaded-documents Azure container (or local dev temp).

    DOCX and PDF files are stored as-is — no conversion is performed.
    A0 handles PDFs natively via ``PDFSourceParser``.

    JSON files must be valid Timed Outline objects (as produced by
    ``POST /documents/generate-to``).  A0 detects the ``.json`` extension and
    uses the fast-path loader, skipping outline re-generation entirely.

    The folder name is derived from the mandatory ``courseTopic`` field (sanitized).
    """
    filename = Path(file.filename or "document.docx").name
    ext = Path(filename).suffix.lower()
    if ext not in _UPLOAD_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only {', '.join(sorted(_UPLOAD_ALLOWED_EXTENSIONS))} files are accepted.",
        )

    folder = _parse_course_topic(course_topic)

    _MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
    try:
        content = await file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read uploaded file — see server logs.",
        ) from exc
    finally:
        await file.close()

    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum allowed upload size is 100 MB.",
        )

    blob_path = _uploads_blob_path(folder, filename)
    document_id = uuid.uuid4().hex[:12]

    from lectora_backend.api.ingestion_status_store import set_status as _set_ingestion_status

    if _azure_storage_ready():
        try:
            _uploads_blob_repo().upload_bytes(
                blob_path,
                content,
                content_type=_CONTENT_TYPES.get(ext, "application/octet-stream"),
            )
        except Exception as exc:
            logger.exception("[upload] Failed to upload to Azure Blob: blob_path=%s", blob_path)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload file to storage — see server logs.",
            ) from exc
        logger.info("[upload] Azure blob %s (%d bytes)", blob_path, len(content))
        # Trigger ingestion for DOCX and PDF files only (skip JSON TO files)
        if ext in {".docx", ".pdf"}:
            _set_ingestion_status(document_id, "pending")
            background_tasks.add_task(
                _run_ingestion_background, content, filename, document_id
            )
        return UploadDocumentResponse(blob_path=blob_path, upload_folder=folder, document_id=document_id)

    slot_dir = _UPLOAD_ROOT / folder
    slot_dir.mkdir(parents=True, exist_ok=True)
    dest = slot_dir / filename
    try:
        dest.write_bytes(content)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save uploaded file: {exc}",
        ) from exc

    logger.info("[upload] Saved %s → %s (%d bytes)", filename, dest, dest.stat().st_size)
    # Trigger ingestion for DOCX and PDF files only (skip JSON TO files)
    if ext in {".docx", ".pdf"}:
        _set_ingestion_status(document_id, "pending")
        background_tasks.add_task(
            _run_ingestion_background, content, filename, document_id
        )
    return UploadDocumentResponse(blob_path=blob_path, upload_folder=folder, document_id=document_id)

async def get_ingestion_status(document_id: str) -> JSONResponse:
    """
    Return the ingestion pipeline status for a document uploaded via POST /documents/upload.

    Status values:
      pending    — queued but not yet started
      processing — parse → chunk → enrich → embed → index in progress
      indexed    — fully embedded and indexed in Azure AI Search
      parsed     — chunked but embedding/indexing was skipped (Azure Search not configured)
      failed     — ingestion encountered an unrecoverable error

    Returns 404 when the document_id is unknown or has expired (4 hr TTL).
    """
    from lectora_backend.api.ingestion_status_store import get_status
    entry = get_status(document_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No ingestion record for document_id: {document_id}",
        )
    return JSONResponse(content=entry)

async def analyze_source(body: SourceAnalysisRequest) -> SourceAnalysisResponse:
    """
    Extract the TOC from an uploaded DOCX or PDF and send it to the LLM for structured
    source analysis.  Call this for every uploaded document before ``POST /documents/generate-to``
    so the TO and LO generation can weight content by source role and extraction focus.

    Returns a :class:`SourceAnalysisResponse` with main topics, recommended course use,
    depth, learning objectives supported, and topics to ignore.
    """
    resolved = _validate_document_path(body.blob_path)
    ext = resolved.suffix.lower()
    if ext not in {".docx", ".pdf"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .docx and .pdf files can be analyzed.",
        )

    # ── Extract TOC entries ───────────────────────────────────────────────────
    toc_lines: list[str] = []
    try:
        if ext == ".docx":
            from lectora_backend.pipeline.agent.a0_request_synthesizer.step_01_document_parsing.utils.doc_parser import (
                CourseDocParser,
            )
            parser = CourseDocParser(docx_paths=[str(resolved)])
            entries = parser.extract_toc_entries()
            for e in entries[:200]:
                indent = "  " * max(0, e.level - 1)
                toc_lines.append(f"{indent}[L{e.level}] {e.text}")
        else:
            from lectora_backend.pipeline.agent.a0_request_synthesizer.step_01_document_parsing.utils.pdf_parser import (
                PDFSourceParser,
            )
            pdf_parser = PDFSourceParser([str(resolved)])
            entries = pdf_parser.extract_toc_entries(include_heading_fallback=True)
            for e in entries[:200]:
                page_suffix = f" p{e.page}" if e.page else ""
                indent = "  " * max(0, e.level - 1)
                toc_lines.append(f"{indent}[L{e.level}] {e.text}{page_suffix}")
    except Exception:
        logger.warning("[analyze-source] TOC extraction failed for %s — proceeding without TOC", resolved.name)

    toc_text = "\n".join(toc_lines) if toc_lines else "(no structured TOC found)"

    # ── Build the LLM prompt ─────────────────────────────────────────────────
    _SOURCE_ANALYSIS_SYSTEM = (
        "You are a course design expert. Analyze a training source document and return structured JSON.\n\n"
        "Return ONLY valid JSON matching this exact schema (no markdown, no prose):\n"
        "{\n"
        '  "main_topics": ["topic1", "topic2", ...],\n'
        '  "recommended_course_use": "one sentence on how to use this source",\n'
        '  "recommended_depth": "light" | "moderate" | "comprehensive",\n'
        '  "supports_learning_objectives": ["LO1", "LO2", ...],\n'
        '  "ignore_or_reduce": ["topic or section to deprioritize", ...]\n'
        "}\n\n"
        "Guidelines:\n"
        "- When the user specifies what to get from this source, prioritise those topics above all else\n"
        "- primary_source → comprehensive depth unless the extraction focus narrows scope\n"
        "- supporting_source → moderate depth, use only for relevant sections\n"
        "- reference_only → light depth, cite for edge cases only\n"
        "- Topics outside the user's extraction focus belong in ignore_or_reduce\n"
        "- Learning objectives should be action-verb (Bloom's taxonomy) statements\n"
        "- 3–8 main_topics, 2–4 supports_learning_objectives, 1–4 ignore_or_reduce entries"
    )

    extract_hint = (body.extract_hint or "").strip()
    user_msg_parts = [
        f"Source file: {resolved.name}",
        f"Source role: {body.source_role}",
    ]
    if extract_hint:
        user_msg_parts.append(f"What should we get from this source:\n{extract_hint}")
    user_msg_parts.append(f"Document TOC:\n{toc_text}")
    user_msg = "\n\n".join(user_msg_parts)

    try:
        from lectora_backend.pipeline.agent.a0_request_synthesizer.config.llm import chat_for_to
        raw_json = chat_for_to(_SOURCE_ANALYSIS_SYSTEM, user_msg)
        data = json.loads(raw_json)
    except Exception as exc:
        logger.exception("[analyze-source] LLM call or JSON parse failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Source analysis LLM call failed: {exc}",
        ) from exc

    return SourceAnalysisResponse(
        source_name=resolved.name,
        source_role=body.source_role,
        importance=_importance_for_source(body.source_role, body.importance),
        extract_hint=extract_hint,
        main_topics=data.get("main_topics", []),
        recommended_course_use=data.get("recommended_course_use", ""),
        recommended_depth=data.get("recommended_depth", "moderate"),
        supports_learning_objectives=data.get("supports_learning_objectives", []),
        ignore_or_reduce=data.get("ignore_or_reduce", []),
    )
