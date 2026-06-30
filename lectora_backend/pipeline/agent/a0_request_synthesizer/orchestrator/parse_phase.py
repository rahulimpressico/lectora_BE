import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..step_01_document_parsing.utils.doc_parser import CourseDocParser
from ..step_01_document_parsing.utils.pdf_parser import PDFSourceParser

logger = logging.getLogger(__name__)


@dataclass
class ParsePhaseResult:
    parser: Any
    pdf_parser: Any
    title: str
    course_id: str | None
    learning_objectives: list[str]
    content_sample: str
    classification_sample: str
    to_outline_content: str
    total_doc_word_count: int
    indexed_content: str
    total_paragraphs: int
    heading_map: list[tuple[int, str, int, str]]
    heading_tree: list[dict]
    pdf_heading_tree: list[dict]
    doc_dir: Path
    images: list[dict]
    to_is_json: bool


def _build_heading_map_from_heading_tree(
    heading_tree: list[dict],
) -> list[tuple[int, str, int, str]]:
    return [
        (
            int(entry["para_idx"]),
            str(entry["text"]),
            int(entry["level"]),
            str(entry.get("source") or "source"),
        )
        for entry in heading_tree
        if entry.get("para_idx") is not None and entry.get("text")
    ]


def execute_parse_phase(synth: Any) -> ParsePhaseResult:
    has_pdf_text = bool(synth.extra_text_contents)
    synth._emit_step("Loading source documents and extracting structure…")
    logger.info(
        "[A0] Parsing %s DOCX source(s), %s PDF source(s)%s...",
        len(synth.docx_paths),
        len(synth.pdf_paths),
        f" + {len(synth.extra_text_contents)} PDF text block(s)" if has_pdf_text else "",
    )

    to_is_json = (
        synth.to_outline_doc_path is not None
        and synth.to_outline_doc_path.lower().endswith(".json")
    )
    to_is_pdf = (
        synth.to_outline_doc_path is not None
        and synth.to_outline_doc_path.lower().endswith(".pdf")
    )
    parser = (
        CourseDocParser(
            docx_paths=synth.docx_paths,
            to_outline_doc_path=None if (to_is_json or to_is_pdf) else synth.to_outline_doc_path,
        )
        if synth.docx_paths
        else None
    )
    pdf_parser = PDFSourceParser(synth.pdf_paths) if synth.pdf_paths else None
    to_outline_pdf_parser = (
        PDFSourceParser([synth.to_outline_doc_path]) if to_is_pdf and synth.to_outline_doc_path else None
    )

    title = (
        (parser.extract_title() if parser else "")
        or (pdf_parser.extract_title() if pdf_parser else "")
        or "Course"
    )
    course_id = (
        (parser.extract_course_id() if parser else None)
        or (pdf_parser.extract_course_id() if pdf_parser else None)
    )

    learning_objectives: list[str] = []
    seen_objectives: set[str] = set()
    for items in (
        parser.extract_merged_learning_objectives() if parser else [],
        pdf_parser.extract_merged_learning_objectives() if pdf_parser else [],
    ):
        for obj in items:
            key = obj.lower()
            if key not in seen_objectives:
                learning_objectives.append(obj)
                seen_objectives.add(key)

    content_parts: list[str] = []
    if parser:
        docx_content = parser.extract_merged_full_content(max_words=8000)
        if docx_content:
            content_parts.append(docx_content)
    if pdf_parser:
        pdf_content = pdf_parser.extract_merged_full_content(max_words=8000)
        if pdf_content:
            content_parts.append(pdf_content)
    content_sample = "\n\n".join(part for part in content_parts if part.strip())

    if synth.extra_text_contents:
        pdf_combined = "\n\n".join(t for t in synth.extra_text_contents if t.strip())
        if pdf_combined:
            content_sample = content_sample + "\n\n" + pdf_combined if content_sample else pdf_combined
            logger.info(
                "[A0] Merged %s PDF text block(s) into combined content.",
                len(synth.extra_text_contents),
            )

    classification_parts: list[str] = []
    if parser:
        sample = parser.extract_content_sample(max_chars=3000)
        if sample:
            classification_parts.append(sample)
    if pdf_parser:
        sample = pdf_parser.extract_content_sample(max_chars=3000)
        if sample:
            classification_parts.append(sample)
    classification_sample = "\n\n".join(classification_parts)

    to_outline_content = ""
    if synth.to_outline_doc_path and not to_is_json:
        to_outline_content = (
            to_outline_pdf_parser.extract_to_outline_text()
            if to_outline_pdf_parser
            else (parser.extract_to_outline_text() if parser else "")
        )
        to_word_count = len(to_outline_content.split())
        logger.info(
            "[A0] TO document extracted: %d words from %s",
            to_word_count,
            Path(synth.to_outline_doc_path).name,
        )
        if not to_outline_content.strip():
            logger.warning(
                "[A0] WARNING — TO document %r extracted to empty string. "
                "The file may use text boxes, SmartArt, or non-paragraph content "
                "that python-docx cannot read. LLM will receive no TO content.",
                Path(synth.to_outline_doc_path).name,
            )

    total_doc_word_count = (
        (parser.count_total_doc_words() if parser else 0)
        + (pdf_parser.count_total_doc_words() if pdf_parser else 0)
    )
    logger.info("[A0] Source doc word count: %s", total_doc_word_count)

    indexed_parts: list[str] = []
    docx_indexed_words = 0
    pdf_indexed_words = 0
    if parser:
        indexed = parser.extract_indexed_content(max_words=None)
        if indexed:
            docx_indexed_words = len(indexed.split())
            indexed_parts.append(indexed)
    if pdf_parser:
        indexed = pdf_parser.extract_indexed_content(max_words=None)
        if indexed:
            pdf_indexed_words = len(indexed.split())
            indexed_parts.append(indexed)
    indexed_content = "\n".join(indexed_parts)

    total_paragraphs = 0
    if parser:
        total_paragraphs += parser.count_paragraphs()
    if pdf_parser:
        total_paragraphs += pdf_parser.count_paragraphs()

    if to_is_json:
        logger.info(
            "[TO MODE] Existing TO detected (pre-generated JSON: %s) — "
            "will load directly from disk, no LLM TO call needed.",
            synth.to_outline_doc_path,
        )
    elif synth.to_outline_doc_path:
        logger.info(
            "[TO MODE] Existing TO detected (%s) — continuing with detected TO.",
            Path(synth.to_outline_doc_path).name,
        )
    else:
        logger.info(
            "[STRUCTURED CONTENT MODE] TO not found — extracted headings and indexed "
            "content will be sent to LLM (DOCX: heading_tree + indexed paragraphs; "
            "PDF: TOC entries + section content) "
            "(duration=%sh, difficulty=%s, target_words=%d).",
            synth.duration_hours,
            synth.difficulty_level,
            synth.calculated_word_count,
        )

    heading_map: list[tuple[int, str, int, str]] = []
    if parser:
        heading_map.extend(parser.get_section_heading_map())
    pdf_heading_tree = pdf_parser.extract_merged_heading_tree() if pdf_parser else []
    if pdf_parser:
        heading_map.extend(_build_heading_map_from_heading_tree(pdf_heading_tree))
    logger.info("[A0] Heading anchors across sources: %s", len(heading_map))

    heading_tree: list[dict] = []
    seen_headings: set[tuple[str, str]] = set()
    for tree in [
        *(parser.extract_merged_heading_tree() if parser else []),
        *pdf_heading_tree,
    ]:
        source = str(tree.get("source") or "")
        key = (source, str(tree["text"]).lower())
        if key in seen_headings:
            continue
        heading_tree.append(tree)
        seen_headings.add(key)
    logger.info("[A0] Heading tree entries: %s", len(heading_tree))

    logger.info("[EXTRACT] ══════════════ SOURCE EXTRACTION SUMMARY ══════════════")
    if parser:
        docx_headings = [h for h in heading_tree if not str(h.get("source", "")).lower().endswith(".pdf")]
        logger.info(
            "[EXTRACT]  DOCX  → %d headings extracted | %d words indexed",
            len(docx_headings),
            docx_indexed_words,
        )
        if docx_headings:
            logger.info("[EXTRACT]  ── DOCX titles ──────────────────────────────────")
            for h in docx_headings:
                indent = "  " * max(0, int(h.get("level", 1)) - 1)
                logger.info(
                    "[EXTRACT]     [L%s] %s%s",
                    h.get("level", "?"),
                    indent,
                    h.get("text", ""),
                )
    else:
        logger.info("[EXTRACT]  DOCX  → (not provided)")

    if pdf_parser:
        pdf_headings = [h for h in heading_tree if str(h.get("source", "")).lower().endswith(".pdf")]
        if not pdf_headings:
            pdf_headings = pdf_heading_tree
        logger.info(
            "[EXTRACT]  PDF   → %d headings/TOC entries | %d words indexed",
            len(pdf_headings),
            pdf_indexed_words,
        )
        if pdf_headings:
            logger.info("[EXTRACT]  ── PDF TOC / headings ─────────────────────────────")
            for h in pdf_headings:
                indent = "  " * max(0, int(h.get("level", 1)) - 1)
                logger.info(
                    "[EXTRACT]     [L%s] %s%s",
                    h.get("level", "?"),
                    indent,
                    h.get("text", ""),
                )
    else:
        logger.info("[EXTRACT]  PDF   → (not provided)")

    total_indexed = docx_indexed_words + pdf_indexed_words
    logger.info(
        "[EXTRACT]  COMBINED → %d total words from %s source(s) "
        "(DOCX: %d | PDF: %d)",
        total_indexed,
        (1 if parser else 0) + (1 if pdf_parser else 0),
        docx_indexed_words,
        pdf_indexed_words,
    )
    logger.info("[EXTRACT] ══════════════════════════════════════════════════════════")

    synth._check_cancelled()
    if synth.course_output_slug:
        stem = synth.course_output_slug
    elif len(synth.docx_paths) == 1:
        stem = Path(synth.docx_paths[0]).stem
    elif len(synth.pdf_paths) == 1 and not synth.docx_paths:
        stem = Path(synth.pdf_paths[0]).stem
    else:
        stem = f"multi_{synth.run_id}"

    doc_dir = synth.output_dir / stem
    doc_dir.mkdir(parents=True, exist_ok=True)
    input_docs_dir = doc_dir / "doc"
    input_docs_dir.mkdir(parents=True, exist_ok=True)

    persisted_inputs: list[str] = []
    for src_path in [*synth.docx_paths, *synth.pdf_paths]:
        src = Path(src_path)
        if not src.exists() or not src.is_file():
            continue
        dest = input_docs_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
            persisted_inputs.append(dest.name)

    if synth.to_outline_doc_path and not to_is_json:
        to_src = Path(synth.to_outline_doc_path)
        if to_src.exists() and to_src.is_file():
            dest = input_docs_dir / to_src.name
            if not dest.exists():
                shutil.copy2(to_src, dest)
                persisted_inputs.append(dest.name)

    if persisted_inputs:
        logger.info(
            "[A0] Persisted %s input file(s) -> %s",
            len(persisted_inputs),
            input_docs_dir,
        )

    images_dir = doc_dir / "images"
    images: list[dict] = []
    prior_state_path = doc_dir / "shared_state.json"
    images_cached = images_dir.exists() and any(True for entry in images_dir.iterdir() if entry.is_file())
    if images_cached:
        synth._emit_step("Reusing previously extracted source images…")
        logger.info("[A0] images_dir already populated — skipping re-extraction (retry cycle).")
        if prior_state_path.exists():
            try:
                with open(prior_state_path, encoding="utf-8") as state_handle:
                    prior = json.load(state_handle)
                images = prior.get("images") or []
                logger.info("[A0] Loaded %d image record(s) from prior shared_state.", len(images))
            except Exception:
                logger.warning("[A0] Could not load images from prior shared_state; continuing with empty list.")
    else:
        synth._emit_step("Extracting source images and preparing prompts…")
        logger.info("[A0] Extracting images...")
        if parser:
            images.extend(parser.extract_all_images(images_dir))
        if pdf_parser:
            images.extend(
                pdf_parser.extract_all_images(
                    images_dir,
                    start_seq=len(images),
                    heading_anchors=pdf_heading_tree if pdf_heading_tree else None,
                )
            )
        logger.info("[A0] Extracted %s images -> %s", len(images), images_dir)

    return ParsePhaseResult(
        parser=parser,
        pdf_parser=pdf_parser,
        title=title,
        course_id=course_id,
        learning_objectives=learning_objectives,
        content_sample=content_sample,
        classification_sample=classification_sample,
        to_outline_content=to_outline_content,
        total_doc_word_count=total_doc_word_count,
        indexed_content=indexed_content,
        total_paragraphs=total_paragraphs,
        heading_map=heading_map,
        heading_tree=heading_tree,
        pdf_heading_tree=pdf_heading_tree,
        doc_dir=doc_dir,
        images=images,
        to_is_json=to_is_json,
    )


def build_paragraphs_by_source(parsed: ParsePhaseResult) -> dict[str, int]:
    paragraphs_by_source: dict[str, int] = {}
    if parsed.parser:
        paragraphs_by_source.update(
            {path.name: len(doc.paragraphs) for path, doc in parsed.parser._sources}
        )
    if parsed.pdf_parser:
        paragraphs_by_source.update(parsed.pdf_parser.paragraphs_by_source())
    return paragraphs_by_source
