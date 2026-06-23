import argparse
import json
from pathlib import Path

from lectora_backend.pipeline.agent.a0_request_synthesizer.utils.pdf_parser import (
    PDFSourceParser,
    normalize_ws,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract DOCX-parser-like fields from a PDF using pypdf."
    )
    parser.add_argument("pdf_path", help="Path to the PDF file")
    parser.add_argument("-o", "--output", help="Optional output JSON path")
    parser.add_argument(
        "--extract-images",
        action="store_true",
        help="Extract embedded PDF images alongside the JSON payload",
    )
    parser.add_argument(
        "--images-dir",
        help="Optional directory where extracted images should be saved",
    )
    parser.add_argument(
        "--no-heading-fallback",
        action="store_true",
        help="Disable heading-based fallback when the PDF has no embedded outline/bookmarks",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path).expanduser().resolve()
    parser_obj = PDFSourceParser([str(pdf_path)])
    doc = parser_obj._docs[0]
    include_heading_fallback = not args.no_heading_fallback
    outline_headings = parser_obj.extract_toc_entries(
        include_heading_fallback=include_heading_fallback
    )
    toc_hierarchy = parser_obj.extract_toc_hierarchy(
        include_heading_fallback=include_heading_fallback
    )
    toc_section_contents = (
        parser_obj.extract_toc_section_contents(outline_headings)
        if outline_headings
        else []
    )
    heading_tree = [
        {
            "level": entry["level"],
            "text": entry["title"],
            "para_idx": entry["para_idx_start"],
            "source": entry.get("source") or pdf_path.name,
        }
        for entry in toc_section_contents
        if entry.get("para_idx_start") is not None
    ] or parser_obj.extract_merged_heading_tree()

    images = []
    images_dir: Path | None = None
    if args.extract_images:
        images_dir = (
            Path(args.images_dir).expanduser().resolve()
            if args.images_dir
            else pdf_path.parent / f"{pdf_path.stem}_images"
        )
        images = parser_obj.extract_all_images(
            images_dir,
            heading_anchors=heading_tree if heading_tree else None,
        )

    toc_source = "outline" if any(doc_item.toc_entries for doc_item in parser_obj._docs) else ""
    if not toc_source and outline_headings:
        toc_source = "heading_fallback"
    if not toc_source:
        toc_source = "none"

    payload = {
        "source_document": pdf_path.name,
        "extracted_inputs": {
            "title": parser_obj.extract_title(),
            "course_id": parser_obj.extract_course_id(),
            "learning_objectives": parser_obj.extract_merged_learning_objectives(),
            "content_sample": parser_obj.extract_content_sample(max_chars=3000),
            "total_doc_word_count": parser_obj.count_total_doc_words(),
            "to_outline_total_word_count": 0,
        },
        "images": images,
        "pdf_parser_artifacts": {
            "source_file": str(pdf_path),
            "indexed_content": parser_obj.extract_indexed_content(max_words=8000),
            "heading_tree": heading_tree,
            "section_heading_map": [
                {
                    "para_idx": item[0],
                    "heading_text": item[1],
                    "heading_level": item[2],
                }
                for item in parser_obj.get_section_heading_map()
            ],
            "toc_source": toc_source,
            "toc_entries": [
                {
                    "level": entry.level,
                    "text": entry.text,
                    "page": entry.page,
                    "source": entry.source,
                }
                for entry in outline_headings
            ],
            "toc_hierarchy": toc_hierarchy,
            "toc_section_contents": toc_section_contents,
            "page_map": [
                {
                    "page": page_num,
                    "para_idx_start": page_blocks[0].para_idx if page_blocks else None,
                    "para_idx_end": page_blocks[-1].para_idx if page_blocks else None,
                }
                for page_num in range(1, len(doc.reader.pages) + 1)
                for page_blocks in [[block for block in doc.blocks if block.page_num == page_num]]
            ],
            "total_paragraphs": parser_obj.count_paragraphs(),
            "metadata": {
                "pdf_title": normalize_ws(getattr(doc.reader.metadata, "title", "") or ""),
                "pdf_author": normalize_ws(getattr(doc.reader.metadata, "author", "") or ""),
                "pdf_subject": normalize_ws(getattr(doc.reader.metadata, "subject", "") or ""),
                "pdf_creator": normalize_ws(getattr(doc.reader.metadata, "creator", "") or ""),
                "page_count": len(doc.reader.pages),
                "has_outline": any(doc_item.toc_entries for doc_item in parser_obj._docs),
                "heading_fallback_enabled": include_heading_fallback,
                "images_dir": str(images_dir) if images_dir else None,
            },
        },
    }

    output_text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        out_path.write_text(output_text, encoding="utf-8")
        print(f"Wrote JSON to: {out_path}")
        if args.extract_images and images_dir is not None:
            print(f"Extracted {len(images)} image(s) to: {images_dir}")
    else:
        print(output_text)


if __name__ == "__main__":
    main()
