from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from lectora_backend.ingestion.chunking.models import (
    BlockType,
    ChunkMetadata,
    CourseChunk,
    DocumentSection,
    DocumentTree,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

MAX_TOKENS = 600
MIN_TOKENS = 80
_WORDS_PER_MINUTE = 200


def _get_encoder():
    try:
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        logger.warning(
            "[chunk_builder] tiktoken not available — falling back to word-count approximation "
            "(1 token ≈ 0.75 words). Install tiktoken for accurate counts."
        )
        return None


def _count_tokens(text: str, encoder) -> int:
    if encoder is None:
        # Approx: GPT tokeniser averages ~0.75 words per token → 1.33 tokens per word
        return max(1, round(len(text.split()) * 1.33))
    return len(encoder.encode(text))


def _estimated_read_min(token_count: int) -> float:
    words = token_count * 0.75
    return round(words / _WORDS_PER_MINUTE, 2)


def _first_page(section: DocumentSection) -> int | None:
    """Return the page number of the first node in the section, if available."""
    for node in section.nodes:
        if node.page_num is not None:
            return node.page_num
    return None


class CourseChunkBuilder:
    """Build CourseChunk objects from a DocumentTree."""

    def __init__(self) -> None:
        self._encoder = _get_encoder()

    def build(self, tree: DocumentTree) -> list[CourseChunk]:
        chunks: list[CourseChunk] = []
        for section in tree.sections:
            section_chunks = self._chunk_section(section, tree.document_id, tree.filename)
            chunks.extend(section_chunks)
        logger.info(
            "[chunk_builder] Built %d chunks from document %s",
            len(chunks),
            tree.document_id,
        )
        return chunks

    def _chunk_section(
        self, section: DocumentSection, document_id: str, source_file: str
    ) -> list[CourseChunk]:
        body_nodes = [
            n for n in section.nodes
            if not (n.block_type == BlockType.HEADING and n.level > 0)
        ]

        if not body_nodes:
            if section.title and section.title != "(Document Root)":
                return [self._make_chunk(document_id, section, section.title, 0, source_file)]
            return []

        full_content = "\n\n".join(n.text for n in body_nodes)
        total_tokens = _count_tokens(full_content, self._encoder)

        if total_tokens <= MAX_TOKENS:
            return [self._make_chunk(document_id, section, full_content, 0, source_file)]

        # Split at paragraph boundaries
        chunks: list[CourseChunk] = []
        current_texts: list[str] = []
        current_tokens = 0
        seq = 0

        for node in body_nodes:
            node_tokens = _count_tokens(node.text, self._encoder)

            if current_tokens + node_tokens > MAX_TOKENS and current_texts:
                raw_text = "\n\n".join(current_texts)
                if _count_tokens(raw_text, self._encoder) >= MIN_TOKENS:
                    chunks.append(self._make_chunk(document_id, section, raw_text, seq, source_file))
                    seq += 1
                current_texts = [node.text]
                current_tokens = node_tokens
            else:
                current_texts.append(node.text)
                current_tokens += node_tokens

        if current_texts:
            raw_text = "\n\n".join(current_texts)
            if _count_tokens(raw_text, self._encoder) >= MIN_TOKENS:
                chunks.append(self._make_chunk(document_id, section, raw_text, seq, source_file))

        return chunks

    def _make_chunk(
        self,
        document_id: str,
        section: DocumentSection,
        raw_text: str,
        seq: int,
        source_file: str,
    ) -> CourseChunk:
        token_count = _count_tokens(raw_text, self._encoder)
        chunk_id = f"chunk_{document_id}_{section.section_id}_{seq:03d}"
        searchable_text = f"{section.title} {source_file} {raw_text[:500]}"

        return CourseChunk(
            chunk_id=chunk_id,
            document_id=document_id,
            section_id=section.section_id,
            title=section.title,
            parent_title=None,
            level=section.level,
            raw_text=raw_text,
            token_count=token_count,
            estimated_read_min=_estimated_read_min(token_count),
            source_file=source_file,
            page_num=_first_page(section),
            metadata=ChunkMetadata(),
            searchable_text=searchable_text,
        )
