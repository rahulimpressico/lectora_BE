from __future__ import annotations
import logging
import uuid

from lectora_backend.ingestion.chunking.models import BlockType, DocumentNode
from lectora_backend.ingestion.parsers.base import BaseDocumentParser

logger = logging.getLogger(__name__)

_A0_PDF_PARSER = (
    "lectora_backend.pipeline.agent"
    ".a0_request_synthesizer"
    ".step_01_document_parsing.utils.pdf_parser"
)


def _infer_heading_level(line: str, numbered_re, all_caps_re) -> int | None:
    """
    Return heading level 1–3 using the same heuristics as A0's PDFSourceParser,
    or None for plain body text.
    """
    stripped = line.strip()
    if not stripped:
        return None

    words = stripped.split()
    word_count = len(words)

    # ALL CAPS short line → level 1 (mirrors _ALL_CAPS_RE in A0)
    if all_caps_re.match(stripped) and word_count < 12:
        return 1

    # Numbered heading: "1." → 1, "1.1" → 2, "1.1.1" → 3
    m = numbered_re.match(stripped)
    if m:
        first_token = stripped.split()[0].rstrip(".")
        dots = first_token.count(".")
        return min(dots + 1, 3)

    # Short line with no trailing punctuation → level 2 heading guess
    if word_count < 8 and stripped[-1] not in ".!?,;:":
        return 2

    return None


class PDFParser(BaseDocumentParser):
    """
    Parse a PDF into DocumentNode objects.

    Heading heuristics (numbered headings, ALL-CAPS) are imported directly from
    A0's PDFSourceParser module to stay consistent with the rest of the pipeline.
    """

    def parse(self, path: str) -> list[DocumentNode]:
        from importlib import import_module
        from pypdf import PdfReader

        a0_pdf = import_module(_A0_PDF_PARSER)
        numbered_re = a0_pdf._NUMBERED_HEADING_RE
        all_caps_re = a0_pdf._ALL_CAPS_RE

        reader = PdfReader(path)
        nodes: list[DocumentNode] = []
        raw_index = 0

        for page_num, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                logger.warning("[pdf_parser] Page %d extraction failed: %s", page_num, exc)
                continue

            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    raw_index += 1
                    continue

                heading_level = _infer_heading_level(stripped, numbered_re, all_caps_re)

                nodes.append(DocumentNode(
                    node_id=uuid.uuid4().hex[:12],
                    block_type=BlockType.HEADING if heading_level else BlockType.PARAGRAPH,
                    level=heading_level or 0,
                    text=stripped,
                    page_num=page_num,
                    raw_index=raw_index,
                ))
                raw_index += 1

        logger.info(
            "[pdf_parser] Parsed %d nodes from %s (%d pages)",
            len(nodes), path, len(reader.pages),
        )
        return nodes
