"""
Content writer — generates study guide content one lesson at a time.

Subtopics of a lesson are sent in one or more LLM calls (batched when there are
many sections — e.g. a single synthetic TO lesson mapping the whole course).
The LLM returns a JSON array — one element per subtopic, in the same order.

Flow per lesson:
  1. Extract source text for every subtopic and cap it at 3× the section
     word-count target (prevents LLM over-generation on rich source docs).
  2. Calculate each subtopic's word count proportionally from the lesson
     word_count in enriched_sections.json.
  3. Call generate_lesson() → one or more LLM calls → JSON arrays concatenated.
  4. Count words in each generated section and attach metadata.
"""

import json
import logging
import re
import time

import json_repair

from ...config.llm import chat
from ..constants.prompts import (
    build_lesson_system_prompt,
    build_lesson_user_message,
)
from ...shared.helpers.text_utils import _strip_fences
from .source_chunker import (
    extract_full_section_text,
    build_prior_summary,
    extract_last_section_tail,
    load_doc_paragraphs,
    count_source_words,
    extract_section_key_points,
)

# Reserved section headings rendered by A2 from metadata (not from LLM generation).
# Any enriched_section whose title matches these is skipped in content generation.
_RESERVED_LESSON_RE = re.compile(
    r"^\s*(\d+(\.\d+)*\s+)?"
    r"(overview|learning\s+objectives?|learning\s+outcomes?|course\s+objectives?|"
    r"summary|assessment|introduction)\s*$",
    re.IGNORECASE,
)


def _is_reserved_lesson(title: str) -> bool:
    return bool(_RESERVED_LESSON_RE.match((title or "").strip()))


logger = logging.getLogger(__name__)

# Single-call payloads with 100+ sections routinely exceed practical limits; the
# model may return [] or invalid JSON. Chunk when a TO lesson maps many sections.
MAX_SECTIONS_PER_LLM_CALL = 20

# When source >> TO target (rich-source mode), cap source text fed to the LLM at
# this multiple of the section's target_word_count.  Giving the LLM 10 × more
# source than it needs to write causes it to over-generate even when the target
# is marked STRICT.  3× provides enough context without inflating the output.
_SOURCE_TO_TARGET_RATIO = 3.0


def _trim_source_to_budget(text: str, target_wc: int) -> str:
    """Cap source text to _SOURCE_TO_TARGET_RATIO × target_wc words."""
    if not text or target_wc <= 0:
        return text
    cap = max(150, int(target_wc * _SOURCE_TO_TARGET_RATIO))
    words = text.split()
    if len(words) <= cap:
        return text
    return " ".join(words[:cap]) + "\n[... source excerpt capped to word budget ...]"


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _parse_llm_json_array(raw: str) -> list[dict]:
    """
    Parse a JSON array from LLM response.

    The LLM is expected to return a bare JSON array. If it wraps the array
    in a dict key (e.g. {"sections": [...]}), the most likely key is unwrapped.
    Falls back to json_repair when the response contains malformed JSON.
    """
    text = _strip_fences(raw)

    def _extract_list(parsed: object) -> list[dict] | None:
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("sections", "results", "data", "content"):
                if isinstance(parsed.get(key), list):
                    return parsed[key]
        return None

    try:
        parsed = json.loads(text)
        result = _extract_list(parsed)
        if result is not None:
            return result
        raise ValueError(f"Expected a JSON array, got {type(parsed).__name__}")
    except json.JSONDecodeError as original_exc:
        logger.warning(
            "[A2] Invalid JSON from LLM — attempting json_repair. "
            "Raw response (first 500 chars): %r",
            raw[:500],
        )
        try:
            repaired = json_repair.repair_json(text, return_objects=True)
            result = _extract_list(repaired)
            if result is not None:
                logger.info("[A2] json_repair successfully recovered malformed content JSON array.")
                return result
            raise ValueError(
                f"json_repair returned {type(repaired).__name__}, expected list or dict with list"
            )
        except Exception as repair_exc:
            raise ValueError(
                f"LLM returned invalid JSON array and repair failed. "
                f"Original error: {original_exc}. "
                f"Repair error: {repair_exc}. "
                f"Raw output (first 500 chars): {raw[:500]!r}"
            ) from original_exc


# ── Word-count helper ─────────────────────────────────────────────────────────

def _count_words_in_section(section_data: dict) -> int:
    """Count total words in a generated section's body_paragraphs."""
    total = 0
    for para in section_data.get("body_paragraphs", []):
        ptype = para.get("type", "")
        if ptype in ("text", "important_callout", "heading_3", "heading_4"):
            total += len(re.findall(r"\w+", para.get("content", "")))
        elif ptype in ("bullet_list", "sub_bullet_list", "numbered_list"):
            for item in para.get("items", []):
                total += len(re.findall(r"\w+", item))
        elif ptype == "knowledge_check":
            total += len(re.findall(r"\w+", para.get("question", "")))
            for opt in para.get("options", []):
                total += len(re.findall(r"\w+", opt))
            total += len(re.findall(r"\w+", para.get("explanation", "")))
        elif ptype == "table":
            for hdr in (para.get("headers") or []):
                total += len(re.findall(r"\w+", str(hdr)))
            for row in (para.get("rows") or []):
                for cell in (row or []):
                    total += len(re.findall(r"\w+", str(cell)))
    return total


# ── Lesson-level generation ───────────────────────────────────────────────────


def _generate_lesson_single_call(
    lesson: dict,
    subtopic_specs: list[dict],
    learning_objectives: list[str],
    prior_summary: str,
    rule_pack: dict,
    lesson_wc: int,
    feedback: str | None,
    retries: int,
    *,
    batch_info: str = "",
    audience: str = "",
    special_instructions: str | None = None,
    prev_lesson_context: str = "",
) -> list[dict]:
    """One LLM round-trip for a slice of subtopic_specs (internal)."""
    if not subtopic_specs:
        return []

    system_prompt = build_lesson_system_prompt(rule_pack, audience=audience)

    user_msg = build_lesson_user_message(
        lesson=lesson,
        subtopic_specs=subtopic_specs,
        learning_objectives=learning_objectives,
        prior_summary=prior_summary,
        rule_constraints=rule_pack,
        lesson_wc=lesson_wc,
        feedback=feedback,
        audience=audience,
        special_instructions=special_instructions,
        prev_lesson_context=prev_lesson_context,
    )

    last_error: str | None = None
    prefix = f"{batch_info} " if batch_info else ""

    for attempt in range(1, retries + 1):
        try:
            raw = chat(system_prompt, user_msg)
            sections_data = _parse_llm_json_array(raw)

            if len(sections_data) != len(subtopic_specs):
                raise ValueError(
                    f"Expected {len(subtopic_specs)} sections in array, "
                    f"got {len(sections_data)}"
                )

            results: list[dict] = []
            for i, sec in enumerate(sections_data):
                if "body_paragraphs" not in sec:
                    raise ValueError(
                        f"Section {i + 1} missing 'body_paragraphs'")
                wc = _count_words_in_section(sec)
                sec["word_count"] = wc
                sec["status"] = "generated"
                sec["attempts"] = attempt
                logger.info(
                    "    %s[%s/%s] %s — %sw (target %sw)",
                    prefix,
                    i + 1,
                    len(subtopic_specs),
                    sec.get("heading", "?"),
                    wc,
                    subtopic_specs[i].get("target_word_count", 0),
                )
                results.append(sec)

            return results

        except Exception as e:
            last_error = str(e)
            logger.warning(
                "  [A2] %sAttempt %s/%s error: %s",
                prefix,
                attempt,
                retries,
                last_error,
            )
            if attempt < retries:
                # Azure OpenAI server errors (500) need recovery time before retry.
                # Short sleep suffices for parse/validation failures (our fault).
                _err_lower = last_error.lower()
                is_server_err = (
                    "500" in last_error
                    or "server_error" in _err_lower
                    or "internal server" in _err_lower
                )
                wait_s = 30 if is_server_err else (2 * attempt)
                if is_server_err:
                    logger.info(
                        "  [A2] %sServer error — waiting %ss for Azure to recover "
                        "before attempt %s/%s…",
                        prefix, wait_s, attempt + 1, retries,
                    )
                time.sleep(wait_s)

    return [
        {
            "heading":         spec.get("heading", f"Section {i + 1}"),
            "body_paragraphs": [],
            "word_count":      0,
            "status":          "failed",
            "error":           last_error,
            "attempts":        retries,
        }
        for i, spec in enumerate(subtopic_specs)
    ]


def generate_lesson(
    lesson: dict,
    subtopic_specs: list[dict],
    learning_objectives: list[str],
    prior_summary: str,
    rule_pack: dict,
    lesson_wc: int,
    max_retries: int | None = None,
    feedback: str | None = None,
    audience: str = "",
    special_instructions: str | None = None,
    prev_lesson_context: str = "",
) -> list[dict]:
    """
    Generate content for ALL subtopics of one TO lesson (one or more LLM calls).

    Large lessons (e.g. entire course under one synthetic TO bucket) are split
    into batches of ``MAX_SECTIONS_PER_LLM_CALL`` so the model returns a valid
    JSON array at practical sizes.

    Args:
        lesson         : The TO lesson entry from enriched_sections.
        subtopic_specs : List of per-subtopic dicts (heading, target_word_count,
                         source_text, has_knowledge_check, …).
        learning_objectives: Full list of course LOs.
        prior_summary  : Brief summary of already-generated lessons.
        rule_pack      : Active rule pack constraints.
        lesson_wc      : Total word budget for this lesson (from enriched_sections.json).
        max_retries    : Retry count on JSON parse failure (default: rule_pack.error_tolerance.max_retries_per_step).

    Returns:
        List of section dicts in the same order as subtopic_specs.
        Each dict has: heading, body_paragraphs, word_count, status, attempts.
        On complete failure, returns stub dicts with status="failed".
    """
    if not subtopic_specs:
        return []

    retries = max_retries
    if retries is None:
        retries = int(rule_pack.get("error_tolerance", {}).get("max_retries_per_step", 3))

    n_specs = len(subtopic_specs)
    if n_specs <= MAX_SECTIONS_PER_LLM_CALL:
        return _generate_lesson_single_call(
            lesson=lesson,
            subtopic_specs=subtopic_specs,
            learning_objectives=learning_objectives,
            prior_summary=prior_summary,
            rule_pack=rule_pack,
            lesson_wc=lesson_wc,
            feedback=feedback,
            retries=retries,
            audience=audience,
            special_instructions=special_instructions,
            prev_lesson_context=prev_lesson_context,
        )

    n_batches = (n_specs + MAX_SECTIONS_PER_LLM_CALL - 1) // MAX_SECTIONS_PER_LLM_CALL
    logger.info(
        "  [A2] Lesson %r: %s section(s) in %s batch(es) (max %s per LLM call)",
        lesson.get("title", ""),
        n_specs,
        n_batches,
        MAX_SECTIONS_PER_LLM_CALL,
    )

    all_results: list[dict] = []
    for b in range(n_batches):
        start = b * MAX_SECTIONS_PER_LLM_CALL
        chunk = subtopic_specs[start : start + MAX_SECTIONS_PER_LLM_CALL]
        chunk_wc = sum(int(s.get("target_word_count") or 0) for s in chunk)
        if chunk_wc <= 0:
            chunk_wc = max(
                200,
                lesson_wc * len(chunk) // max(n_specs, 1),
            )
        batch_label = f"batch {b + 1}/{n_batches}"
        # Only pass prev_lesson_context to the first batch — subsequent batches
        # already have continuity from the sections generated before them.
        batch_prev_ctx = prev_lesson_context if b == 0 else ""
        all_results.extend(
            _generate_lesson_single_call(
                lesson=lesson,
                subtopic_specs=chunk,
                learning_objectives=learning_objectives,
                prior_summary=prior_summary,
                rule_pack=rule_pack,
                lesson_wc=chunk_wc,
                feedback=feedback,
                retries=retries,
                batch_info=batch_label,
                audience=audience,
                special_instructions=special_instructions,
                prev_lesson_context=batch_prev_ctx,
            )
        )

    return all_results


# ── Entry point ───────────────────────────────────────────────────────────────

def generate_all_sections(
    enriched_sections: list[dict],
    docx_path: str,
    learning_objectives: list[str],
    rule_pack: dict,
    feedback: str | None = None,
    source_chunks: list[dict] | None = None,
    shared_state_path: str | None = None,
    audience: str = "",
    special_instructions: str | None = None,
) -> list[dict]:
    """
    Generate content for every lesson in enriched_sections.

    For each TO lesson, all subtopics are sent in a SINGLE LLM call.
    Word count per subtopic is distributed proportionally based on the length
    of each subtopic's source text, within the lesson's total word_count budget
    from enriched_sections.json.

    Args:
        enriched_sections : List of TO-lesson dicts from Section Mapper.
        docx_path         : Path to the source .docx file (DOCX or PDF).
        learning_objectives: Full list of course learning objectives.
        rule_pack         : Active rule pack constraints.
        feedback          : Optional S2 feedback to inject into generation.
        source_chunks     : Optional pre-built chunk list from chunk_files().
                            When provided, topic-wise retrieval supplements
                            paragraph-range extraction for each subtopic.
        shared_state_path : Path to shared_state.json.  Required when
                            ``docx_path`` is a PDF so that paragraphs can be
                            reconstructed from ``indexed_content``.

    Returns:
        Flat list of generated section dicts in document order.
    """
    generated: list[dict] = []
    total_lessons = len(enriched_sections)

    try:
        doc_paragraphs = load_doc_paragraphs(docx_path, shared_state_path=shared_state_path)
    except (OSError, ValueError) as exc:
        logger.warning("  [A2] Could not load doc paragraphs: %s", exc)
        doc_paragraphs = []

    # Import chunk retrieval only when chunks are available (avoids hard dep)
    _build_context = None
    if source_chunks:
        try:
            from lectora_backend.pipeline.agent.a0_request_synthesizer.utils.chunker import (
                build_context_for_topic,
            )
            _build_context = build_context_for_topic
            logger.info("[A2] Multi-file chunk index loaded: %d chunks available.", len(source_chunks))
        except ImportError:
            logger.warning("[A2] chunker not available — falling back to paragraph-range source.")
            source_chunks = None

    for lesson_idx, lesson in enumerate(enriched_sections, start=1):
        to_title = lesson.get("title", "")
        to_content = lesson.get("content", "")
        to_ie = lesson.get("interactive_elements", [])
        subtopics = lesson.get("subtopics", [])

        try:
            lesson_wc = int(float(lesson.get("word_count") or 500))
        except (ValueError, TypeError):
            lesson_wc = 500

        try:
            lesson_mins = float(lesson.get("minutes") or 3.0)
        except (ValueError, TypeError):
            lesson_mins = 3.0

        logger.info(
            "[Lesson %s/%s] %s  (%sw, %s subtopic(s))",
            lesson_idx,
            total_lessons,
            to_title,
            lesson_wc,
            len(subtopics),
        )

        if not subtopics:
            logger.info("  -> No subtopics, skipping lesson")
            continue

        # Reserved sections (Overview, Learning Objectives, Summary, etc.) are
        # rendered by doc_formatter from extracted metadata — never generated here.
        if _is_reserved_lesson(to_title):
            logger.info(
                "  -> Reserved section %r — skipping content generation "
                "(rendered from metadata by doc_formatter).",
                to_title,
            )
            continue

        # ── Word-count budget per subtopic ────────────────────────────────────
        # Format 1 (breakdown): each subtopic object carries its own word_count
        #   from the TO document → use it directly as the generation target.
        # Format 2 (flat): subtopics have no TO word_count → distribute the
        #   lesson budget proportionally based on source-text length (current).
        def _parse_to_wc(val) -> int:
            try:
                f = float(val)
                return int(f) if f > 0 else 0
            except (TypeError, ValueError):
                return 0

        to_wc_values = [_parse_to_wc(sub.get("word_count")) for sub in subtopics]
        use_to_wc    = any(v > 0 for v in to_wc_values)

        if use_to_wc:
            # Format 1 — use TO subtopic word_count; fall back to even split for zeros
            fallback = max(50, lesson_wc // len(subtopics))
            wc_per_sub = [v if v > 0 else fallback for v in to_wc_values]
            logger.debug("  [A2] Using TO subtopic word_counts: %s", wc_per_sub)
        else:
            # Format 2 — proportional distribution from source-text length
            src_wc: list[int] = []
            for sub in subtopics:
                src_wc.append(
                    count_source_words(
                        doc_paragraphs,
                        sub.get("para_start", 0),
                        sub.get("para_end", 0),
                    )
                )
            total_src = sum(src_wc)
            if total_src > 0:
                wc_per_sub = [
                    max(50, int(lesson_wc * w / total_src)) if w > 0 else 50
                    for w in src_wc
                ]
            else:
                even = max(50, lesson_wc // len(subtopics))
                wc_per_sub = [even] * len(subtopics)

        max_page = int((rule_pack.get("lectora_constraints") or {}).get("max_words_per_page") or 0)
        if max_page > 0:
            wc_per_sub = [min(w, max_page) for w in wc_per_sub]

        # ── Build subtopic_specs (extract source text for each subtopic) ──────
        subtopic_specs: list[dict] = []

        # Parent overview: only when (a) the lesson has its own non-zero
        # word_count budget AND (b) the subtopics carry their own TO word_count
        # (Format 1).  In that case lesson_wc is the parent intro budget,
        # NOT a sum to distribute, and the parent heading would otherwise
        # never be written into the docx.
        parent_overview_added = False
        if use_to_wc and lesson_wc > 0:
            first_sub_para_start = min(
                (sub.get("para_start", 0) for sub in subtopics if sub.get("para_start")),
                default=0,
            )
            # Pull the few paragraphs leading up to the first subtopic — this
            # is the parent's intro text in the source doc.
            intro_start = max(0, first_sub_para_start - 12)
            intro_end   = max(intro_start, first_sub_para_start - 1)
            parent_source = extract_full_section_text(
                doc_paragraphs,
                para_start=intro_start,
                para_end=intro_end,
            )
            # Aggregate LO mappings from all child subtopics so the intro
            # frames the lesson against the relevant LOs.
            agg_objectives: list[int] = []
            for sub in subtopics:
                for o in (sub.get("maps_to_objectives") or []):
                    if o not in agg_objectives:
                        agg_objectives.append(o)

            parent_wc = lesson_wc
            if max_page > 0:
                parent_wc = min(parent_wc, max_page)
            parent_source = _trim_source_to_budget(parent_source, parent_wc)
            subtopic_specs.append({
                "heading":              to_title,
                "target_word_count":    parent_wc,
                "source_text":          parent_source,
                "has_knowledge_check":  False,
                "maps_to_objectives":   agg_objectives,
                "subtopics":            [sub.get("title", "") for sub in subtopics],
                "interactive_elements": [],
                "image_count":          0,
                "target_minutes":       lesson_mins,
                "_is_parent_overview":  True,
                # First spec of the lesson — inter-lesson bridging is handled via
                # prev_lesson_context at the lesson level; no intra-lesson prev here.
                "prev_section_heading": "",
            })
            parent_overview_added = True

        # Track what immediately precedes each subtopic for transition context.
        # When a parent overview was prepended it is the first section, so the
        # first subtopic's "previous" is the lesson title (overview heading).
        # Without a parent overview, the first subtopic has no intra-lesson prev
        # (inter-lesson bridging is already handled via prev_lesson_context).
        _prev_spec_heading: str = to_title if parent_overview_added else ""

        for sub_i, sub in enumerate(subtopics):
            source_text = extract_full_section_text(
                doc_paragraphs,
                para_start=sub.get("para_start", 0),
                para_end=sub.get("para_end", 0),
            )

            # If the Section Mapper pre-fetched vector-retrieved chunks for this
            # subtopic, use them as the primary source text.
            # The paragraph-range text is appended as supplementary ONLY when it
            # contains meaningful content (para_start != 0 or para_end != 0).
            # When both are 0 the subtopic was built from the TO outline alone
            # (new vector-retrieval architecture) and has no meaningful para span.
            matched_chunks = sub.get("matched_chunks") or []
            if matched_chunks:
                # Deduplicate and merge chunks in document order into a single block.
                seen_texts: set[str] = set()
                merged_parts: list[str] = []
                for c in matched_chunks:
                    t = (c.get("raw_text") or "").strip()
                    if t and t not in seen_texts:
                        merged_parts.append(t)
                        seen_texts.add(t)
                vector_text = "\n\n".join(merged_parts)

                if vector_text:
                    para_start = sub.get("para_start", 0)
                    para_end = sub.get("para_end", 0)
                    has_para_range = para_start != 0 or para_end != 0

                    if source_text and has_para_range:
                        source_text = (
                            f"{vector_text}"
                            "\n\n--- Supplementary paragraph range ---\n"
                            f"{source_text}"
                        )
                    else:
                        source_text = vector_text

                    logger.debug(
                        "[A2] Subtopic=%r — %d vector chunk(s) (%.0f chars)%s",
                        sub.get("title", "")[:40],
                        len(matched_chunks),
                        len(vector_text),
                        "; para-range appended" if (source_text and has_para_range) else "",
                    )

            # When a multi-file chunk index is available, retrieve topic-relevant
            # passages from all source files and append them to the source text.
            # This ensures A2 uses content from PDFs and additional DOCXs without
            # sending every file to every LLM call.
            if _build_context and source_chunks:
                sub_heading = sub.get("title", "")
                query = f"{to_title} {sub_heading}".strip()
                chunk_ctx = _build_context(query, source_chunks, top_k=6, max_words=1500)
                if chunk_ctx:
                    separator = "\n\n--- Additional source material ---\n"
                    source_text = f"{source_text}{separator}{chunk_ctx}" if source_text else chunk_ctx
            source_text = _trim_source_to_budget(source_text, wc_per_sub[sub_i])
            sub_heading = sub.get("title", f"Section {sub_i + 1}")
            subtopic_specs.append({
                "heading":             sub_heading,
                "target_word_count":   wc_per_sub[sub_i],
                "source_text":         source_text,
                "has_knowledge_check": sub.get("has_knowledge_check", False),
                "maps_to_objectives":  sub.get("maps_to_objectives", []),
                "subtopics":           sub.get("subtopics", []),
                # Use only the section-level IE — do NOT fall back to the lesson (TO outline) IE.
                # If a section does not have an element in its own interactive_elements,
                # that element will not be generated for that section.
                "interactive_elements": sub.get("interactive_elements", []),
                "image_count":         sub.get("image_count", 0),
                "target_minutes":      lesson_mins,
                # Intra-lesson continuity: heading of the section that immediately
                # precedes this one in the batch. Empty for the first section of a
                # lesson when no parent overview exists (inter-lesson context is
                # handled separately via prev_lesson_context).
                "prev_section_heading": _prev_spec_heading,
            })
            _prev_spec_heading = sub_heading

        # ── Single LLM call for the whole lesson ──────────────────────────────
        prior_summary = build_prior_summary(generated)
        prev_lesson_context = extract_last_section_tail(generated)
        results = generate_lesson(
            lesson=lesson,
            subtopic_specs=subtopic_specs,
            learning_objectives=learning_objectives,
            prior_summary=prior_summary,
            rule_pack=rule_pack,
            lesson_wc=lesson_wc,
            feedback=feedback,
            audience=audience,
            special_instructions=special_instructions,
            prev_lesson_context=prev_lesson_context,
        )

        # ── Attach metadata and collect ───────────────────────────────────────
        # When a parent overview was prepended, results[0] corresponds to it
        # and results[1..] map back to subtopics[0..]. Otherwise they are 1:1.
        offset = 1 if parent_overview_added else 0
        for i, result in enumerate(results):
            if i == 0 and parent_overview_added:
                # Parent overview block — major section heading (level 1 = N.0)
                result["subtopics"] = [sub.get("title", "") for sub in subtopics]
                result["maps_to_objectives"] = []
                result["section_id"] = ""
                result["images"] = []
                result["outline_lesson"] = to_title
                result["is_parent_overview"] = True
                result["level"] = 1
                generated.append(result)
            else:
                sub_idx = i - offset
                if sub_idx < 0 or sub_idx >= len(subtopics):
                    continue
                sub = subtopics[sub_idx]
                result["subtopics"] = sub.get("subtopics", [])
                result["maps_to_objectives"] = sub.get("maps_to_objectives", [])
                result["section_id"] = sub.get("id", "")
                result["images"] = sub.get("images", [])
                result["outline_lesson"] = to_title
                result["level"] = 2  # sub-section heading (N.M)
                generated.append(result)

            status = result.get("status", "unknown")
            wc_out = result.get("word_count", 0)
            if status == "failed":
                logger.error(
                    "    -> FAILED [%s]: %s",
                    result.get("heading", ""),
                    result.get("error", "?"),
                )
            else:
                logger.info("    -> %s — %sw", result.get("heading", ""), wc_out)

    return generated
