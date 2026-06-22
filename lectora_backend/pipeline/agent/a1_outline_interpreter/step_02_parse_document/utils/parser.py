"""Step 02 — parse_document LangGraph node."""
import logging
import os as _os

from ...shared.models.state import A1State
from .docx_parser import parse_docx_document
from .pdf_parser import _parse_pdf_sections_from_shared_state

logger = logging.getLogger(__name__)


def parse_document(state: A1State) -> A1State:
    if state["status"] == "failed":
        return state

    attempt = state.get("retry_count", 0) + 1
    logger.info("[A1] Parsing document (attempt %s)...", attempt)

    try:
        docx_path = state["docx_path"]

        if docx_path.lower().endswith(".pdf"):
            logger.info(
                "[A1] PDF source detected — rebuilding sections from "
                "shared-state heading_tree + indexed_content."
            )
            a0_data = state.get("a0_data", {})
            sections, total_words, kc_count = _parse_pdf_sections_from_shared_state(a0_data)
            logger.info(
                "[A1] Reconstructed %s sections, %s words, %s KC(s) from PDF shared state.",
                len(sections), total_words, kc_count,
            )
        else:
            if not _os.path.exists(docx_path):
                raise FileNotFoundError(f"Source document not found: {docx_path!r}")
            sections, total_words, kc_count = parse_docx_document(docx_path)
            logger.info(
                "[A1] Parsed %s sections, %s words, %s knowledge checks.",
                len(sections), total_words, kc_count,
            )

        return {
            **state,
            "raw_sections": sections,
            "total_word_count": total_words,
            "kc_count": kc_count,
            "error": None,
        }
    except Exception as e:
        rc = state.get("retry_count", 0)
        return {
            **state,
            "retry_count": rc + 1,
            "error": f"parse_document: {e}",
            "status": "running" if rc < 1 else "failed",
        }
