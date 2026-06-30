import logging
from dataclasses import dataclass
from typing import Any

from ..step_01_document_parsing.utils.doc_parser import CourseDocParser
from ..step_01_document_parsing.utils.pdf_parser import PDFSourceParser
from .parse_phase import ParsePhaseResult

logger = logging.getLogger(__name__)

GENERIC_TITLES: frozenset[str] = frozenset(
    {
        "",
        "course",
        "untitled",
        "document",
        "module",
        "lesson",
        "training",
        "presentation",
        "content",
        "material",
        "study",
        "guide",
    }
)


@dataclass
class ClassificationPhaseResult:
    title: str
    all_doc_titles: list[str]
    rich_classification_sample: str


def _clean_title_list(titles: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for raw in titles:
        title = raw.strip()
        if not title:
            continue
        if title.lower() in GENERIC_TITLES:
            continue
        seen[title] = None
    return list(seen)


def _collect_titles_for_sources(synth: Any, parsed: ParsePhaseResult) -> list[str]:
    raw_titles: list[str] = []
    if parsed.parser:
        for docx_path in synth.docx_paths:
            try:
                parser = CourseDocParser(docx_paths=[str(docx_path)])
                title = parser.extract_title()
                if title and title.strip():
                    raw_titles.append(title.strip())
            except Exception:
                pass
    if parsed.pdf_parser:
        for pdf_path in synth.pdf_paths:
            try:
                parser = PDFSourceParser([str(pdf_path)])
                title = parser.extract_title()
                if title and title.strip():
                    raw_titles.append(title.strip())
            except Exception:
                pass
    if not raw_titles and parsed.title:
        raw_titles = [parsed.title]
    return raw_titles


def prepare_classification_phase(
    synth: Any,
    parsed: ParsePhaseResult,
) -> ClassificationPhaseResult:
    raw_titles = _collect_titles_for_sources(synth, parsed)
    classify_all_titles = _clean_title_list(raw_titles)
    if not classify_all_titles and parsed.title:
        classify_all_titles = [parsed.title]
    logger.info(
        "[A0] Classification titles (cleaned, %d of %d raw): %s",
        len(classify_all_titles),
        len(raw_titles),
        classify_all_titles,
    )

    title = parsed.title
    if (not title or title.lower() in GENERIC_TITLES) and classify_all_titles:
        title = classify_all_titles[0]
        logger.info("[A0] Primary title upgraded to: %r", title)

    classify_parts: list[str] = []
    if parsed.parser:
        sample = parsed.parser.extract_content_sample(max_chars=8000)
        if sample:
            classify_parts.append(sample)
    if parsed.pdf_parser:
        sample = parsed.pdf_parser.extract_content_sample(max_chars=8000)
        if sample:
            classify_parts.append(sample)
    rich_classification_sample = "\n\n".join(classify_parts) or parsed.classification_sample

    return ClassificationPhaseResult(
        title=title,
        all_doc_titles=classify_all_titles,
        rich_classification_sample=rich_classification_sample,
    )
