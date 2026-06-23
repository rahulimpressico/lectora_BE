from __future__ import annotations
import logging
import uuid
from pathlib import Path

from lectora_backend.ingestion.chunking.models import (
    BlockType,
    DocumentNode,
    DocumentSection,
    DocumentTree,
)

logger = logging.getLogger(__name__)


class DocumentStructureExtractor:
    """Extract a hierarchical DocumentTree from a DOCX or PDF file."""

    def extract(self, file_path: str, document_id: str) -> DocumentTree:
        path = Path(file_path)
        ext = path.suffix.lower()
        filename = path.name

        if ext == ".docx":
            from lectora_backend.ingestion.parsers.docx_parser import DOCXParser
            parser = DOCXParser()
            file_type = "docx"
        elif ext == ".pdf":
            from lectora_backend.ingestion.parsers.pdf_parser import PDFParser
            parser = PDFParser()
            file_type = "pdf"
        else:
            raise ValueError(f"Unsupported file type: {ext}. Only .docx and .pdf are supported.")

        flat_nodes = parser.parse(file_path)

        # Compute total_pages for docx (no page info) vs pdf
        total_pages = 0
        if file_type == "pdf":
            page_nums = [n.page_num for n in flat_nodes if n.page_num is not None]
            total_pages = max(page_nums) if page_nums else 0
        else:
            total_pages = 0

        sections = self._build_hierarchy(flat_nodes, document_id)

        tree = DocumentTree(
            document_id=document_id,
            filename=filename,
            file_type=file_type,
            total_pages=total_pages,
            sections=sections,
            flat_nodes=flat_nodes,
        )
        logger.info(
            "[structure_extractor] Extracted %d sections, %d nodes from %s",
            len(sections),
            len(flat_nodes),
            filename,
        )
        return tree

    def _build_hierarchy(
        self, flat_nodes: list[DocumentNode], document_id: str
    ) -> list[DocumentSection]:
        """Build a tree of DocumentSection objects using a stack-based algorithm."""
        sections: list[DocumentSection] = []
        # Stack holds open sections (section_id, level)
        stack: list[DocumentSection] = []
        section_map: dict[str, DocumentSection] = {}

        # Create a root section to capture body nodes before any heading
        root_section = DocumentSection(
            section_id=f"{document_id}_root",
            title="(Document Root)",
            level=0,
            parent_id=None,
            children=[],
            nodes=[],
            para_start=0,
            para_end=0,
        )
        sections.append(root_section)
        section_map[root_section.section_id] = root_section
        stack.append(root_section)

        for node in flat_nodes:
            if node.block_type == BlockType.HEADING and node.level > 0:
                # Pop stack until we find a section with shallower level
                while stack and stack[-1].level >= node.level and stack[-1].section_id != root_section.section_id:
                    closed = stack.pop()
                    # Update para_end on close
                    closed.para_end = node.raw_index

                parent = stack[-1] if stack else root_section
                new_section = DocumentSection(
                    section_id=f"{document_id}_{uuid.uuid4().hex[:8]}",
                    title=node.text,
                    level=node.level,
                    parent_id=parent.section_id,
                    children=[],
                    nodes=[node],
                    para_start=node.raw_index,
                    para_end=node.raw_index,
                )
                parent.children.append(new_section.section_id)
                sections.append(new_section)
                section_map[new_section.section_id] = new_section
                stack.append(new_section)
            else:
                # Body node — append to topmost section
                if stack:
                    current = stack[-1]
                    current.nodes.append(node)
                    current.para_end = node.raw_index

        # Close remaining open sections
        while stack:
            closed = stack.pop()
            if not closed.nodes and closed.section_id != root_section.section_id:
                # Update para_end to last node raw_index if available
                if flat_nodes:
                    closed.para_end = flat_nodes[-1].raw_index

        # Remove root section if it has no meaningful content (only headings)
        body_nodes_in_root = [n for n in root_section.nodes if n.block_type != BlockType.HEADING]
        if not body_nodes_in_root and not root_section.children:
            sections = [s for s in sections if s.section_id != root_section.section_id]

        return sections
