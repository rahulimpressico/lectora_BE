"""
A2 — Content Generator

Expects Section Mapper to have already run and written enriched_sections
into shared_state["agent_outputs"]["section_map"]["enriched_sections"].

enriched_sections shape (one entry per TO lesson):
  {
    "title":               str,   # TO lesson title
    "content":             str,   # TO Content Objective
    "word_count":          str,   # raw string, e.g. "4115"
    "minutes":             str,
    "credit_hour":         str,
    "interactive_elements": list,
    "subtopics": [
      {
        "title":              str,   # course_spec heading
        "id":                 str,
        "level":              int,
        "is_knowledge_check": bool,
        "has_knowledge_check":bool,
        "para_start":         int,
        "para_end":           int,
        "subtopics":          list,
        "maps_to_objectives": list,
        "images":             list,
        "image_count":        int,
        "interactive_elements": list,
      }, ...
    ]
  }

Flow:
  1. Load shared_state; read section_map enriched_sections.
  2. Generate study guide content lesson-by-lesson, subtopic-by-subtopic.
  3. Render a styled .docx.
  4. Persist A2 output and update shared_state.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from lectora_backend.pipeline.models import A2Output, A2Stats
from lectora_backend.pipeline.rule_pack_config.rule_packs import resolve_rule_pack
from lectora_backend.pipeline.shared_llm_config.llm import chat as llm_chat
from ..config.llm import AGENT_CONFIG, CONCLUSION_CONFIG
from ..step_03_conclusion.constants.prompts import (
    CONCLUSION_SYSTEM_PROMPT,
    build_conclusion_user_message,
)
from ..step_01_generate_content.utils.content_writer import generate_all_sections
from ..step_04_render_docx.utils.doc_formatter import build_study_guide_docx
from ..shared.helpers.text_utils import _strip_fences

logger = logging.getLogger(__name__)

# Target length for prompts/logging — short LLM replies are still kept (no template fallback).
def _build_course_conclusion(
    course_title: str,
    *,
    content_sample: str | None = None,
    learning_objectives: list[str] | None = None,
    generated_sections: list[dict] | None = None,
) -> str:
    """Generate the final Conclusion section via LLM (grounded in source + outline)."""
    if not (content_sample or "").strip():
        logger.warning("[A2] No content_sample — conclusion left empty.")
        return ""

    title_for_prompt = (course_title or "").strip() or "Untitled Course"
    user_msg = build_conclusion_user_message(
        title_for_prompt,
        content_sample or "",
        learning_objectives=learning_objectives or [],
        generated_sections=generated_sections or [],
    )

    try:
        raw = llm_chat(
            CONCLUSION_SYSTEM_PROMPT,
            user_msg,
            config=CONCLUSION_CONFIG,
            agent="A2",
        )
        text = _strip_fences(raw)
        if not text:
            logger.warning("[A2] LLM returned empty conclusion after strip.")
            return ""
        logger.info("[A2] Conclusion via LLM (%s words).", len(text.split()))
        return text
    except Exception as exc:
        logger.warning("[A2] Conclusion LLM failed (%s) — leaving empty.", exc)
        return ""


def _build_capstone_exercise(
    course_title: str,
    learning_objectives: list[str],
    generated_sections: list[dict],
    rule_pack: dict,
) -> dict | None:
    """
    Generate a comprehensive capstone exercise that integrates all major topics.

    The capstone:
    - Opens with a realistic scenario that spans multiple course sections.
    - Contains 3–5 knowledge check questions testing practical application.
    - Maps to all learning objectives collectively.
    - Is appended as the final content section before the conclusion.
    """
    major_topics = [
        s.get("heading", "").strip()
        for s in generated_sections
        if s.get("level", 2) == 1 and s.get("heading")
        and s.get("status") != "failed"
    ][:10]

    if not major_topics:
        logger.info("[A2] Skipping capstone — no major topic sections found.")
        return None

    family = rule_pack.get("family") or ""
    if not family:
        logger.warning("[A2] Capstone: rule_pack missing 'family' — skipping capstone generation.")
        return None
    los = (learning_objectives or [])[:10]

    topics_list = "\n".join(f"- {t}" for t in major_topics)
    los_list = "\n".join(f"- {lo}" for lo in los) if los else "(No explicit learning objectives)"

    system = f"""\
You are a professional continuing education course author for RegEd Inc. ({family}).

Generate a capstone exercise that integrates concepts from all major sections of the course.
Return ONLY a valid JSON object with exactly this structure — no markdown fences, no extra keys:

{{
  "heading": "Capstone Exercise",
  "body_paragraphs": [
    {{"type": "text", "content": "2-3 sentence scenario introduction describing a realistic situation the learner must navigate using knowledge from across the course."}},
    {{
      "type": "knowledge_check",
      "question": "Scenario-based question drawing on Topic A and Topic B...",
      "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
      "correct_answer": "B",
      "explanation": "(A) Wrong because... (B) Correct because... (C) Wrong because... (D) Wrong because..."
    }},
    ... (3-5 knowledge_check blocks total)
  ]
}}

Rules:
- The scenario intro must feel like a real professional situation (claim, client meeting, compliance review, suitability analysis — matching the course domain).
- Every knowledge_check must test PRACTICAL APPLICATION of a concept, not recall of a definition.
- Each question must draw on a DIFFERENT major topic so coverage is broad.
- ALL options must have distinct, plausible-but-wrong distractors. The explanation must address every option.
- Questions must collectively cover the full range of learning objectives.
- Do not repeat question formats — vary the scenario setup for each question.
"""

    user = f"""Course: {course_title}

Major Topics Covered:
{topics_list}

Learning Objectives to validate:
{los_list}

Generate the capstone exercise now. Return ONLY the JSON object."""

    try:
        raw = llm_chat(
            system,
            user,
            config=CONCLUSION_CONFIG,
            agent="A2",
        )
        text = _strip_fences(raw)
        data = json.loads(text)
        if not isinstance(data, dict) or "body_paragraphs" not in data:
            raise ValueError("Capstone LLM returned unexpected structure")

        data.setdefault("heading", "Capstone Exercise")
        data["level"] = 1
        data["status"] = "generated"
        data["word_count"] = sum(
            len(str(bp.get("content", "") or bp.get("question", "")).split())
            for bp in data.get("body_paragraphs", [])
        )
        data["images"] = []
        data["maps_to_objectives"] = list(range(len(los)))
        data["section_id"] = ""
        data["outline_lesson"] = "Capstone"
        data["is_capstone"] = True
        logger.info(
            "[A2] Capstone exercise generated (%s paragraphs, ~%s words).",
            len(data.get("body_paragraphs", [])),
            data["word_count"],
        )
        return data
    except Exception as exc:
        logger.warning("[A2] Capstone exercise generation failed (%s) — skipping.", exc)
        return None


class A2ContentGenerator:
    """
    A2 — Content Generator

    Reads Section Mapper enriched_sections from shared state, generates study guide
    content section-by-section (one section per course_spec section), then renders
    a .docx.
    """

    def __init__(
        self,
        shared_state_path: str,
        docx_path: str,
        output_dir: str = "",
        render_docx: bool = True,
        feedback: str | None = None,
        course_difficulty: str | None = None,
        source_file_paths: list[str] | None = None,
    ):
        """
        Args:
            shared_state_path: Path to the run's shared_state.json.
            docx_path:         Path to the source study guide DOCX (used by content_writer).
            output_dir:        Where to write generated_content.json + study_guide.docx.
            render_docx:       When False, the docx is NOT built — pipeline must call
                               ``render_study_guide_from_state(...)`` after S2 passes.
            feedback:          Optional human-readable feedback (typically derived
                               from a prior S2 validation report) injected into the
                               lesson user prompt so the LLM can address known issues.
            course_difficulty: ``basic``, ``intermediate``, or ``advanced`` — merged
                               into the rule pack via ``resolve_rule_pack``.
            source_file_paths: Optional list of local file paths (DOCX + PDF) used as
                               source material. When provided (or found in shared_state),
                               A2 builds a chunk index and enriches each subtopic prompt
                               with topic-relevant passages retrieved from all source files.
        """
        self.shared_state_path = shared_state_path
        self.docx_path = docx_path
        self.output_dir = Path(output_dir) if output_dir else Path(shared_state_path).expanduser().resolve().parent
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.render_docx = render_docx
        self.feedback = feedback
        self.course_difficulty = course_difficulty
        self.source_file_paths = source_file_paths

    def run(self) -> A2Output:
        """Execute the full A2 pipeline and return a typed A2Output."""

        # -- Step 1: Load shared state ----------------------------------------
        logger.info("[A2] Loading shared state...")
        with open(self.shared_state_path) as f:
            shared_state = json.load(f)

        run_id = shared_state["run_id"]

        # Verify Section Mapper ran successfully
        sm_output = shared_state.get("agent_outputs", {}).get("section_map")
        if not sm_output or sm_output.get("status") != "complete":
            raise RuntimeError(
                "section_map output not found or incomplete in shared state — "
                "run Section Mapper before A2"
            )

        enriched_sections: list[dict] = sm_output.get("enriched_sections", [])
        if not enriched_sections:
            raise RuntimeError("Section Mapper produced no enriched_sections — nothing to generate")

        # Extract metadata — llm_to_outline_classification is the single source of
        # truth for course_title, description, and learning_objectives.
        # It holds the exact TO the LLM generated (or the user's edited TO when
        # to_override was provided) and must flow to the DOCX unchanged.
        extracted = shared_state.get("extracted_inputs", {})
        content_sample = extracted.get("content_sample", "")

        llm_to: dict = shared_state.get("llm_to_outline_classification") or {}
        course_title: str = (
            llm_to.get("course_title")
            or extracted.get("title")
            or shared_state.get("agent_outputs", {}).get("A1", {})
                .get("course_spec", {}).get("course_title", "Untitled Course")
        )
        course_description: str = (llm_to.get("description") or "").strip()
        learning_objectives: list[str] = list(llm_to.get("learning_objectives") or [])

        # Fallback: if the TO had no LOs, use what A0 extracted from the source doc.
        if not learning_objectives:
            learning_objectives = list(extracted.get("learning_objectives") or [])

        # Read user-provided audience and special instructions (stored by local_jobs.py)
        course_audience: str = (shared_state.get("course_audience") or "").strip()
        special_instructions: str | None = (shared_state.get("special_instructions") or "").strip() or None
        # Wizard onboarding config — used only for prompt tone/depth/style guidance,
        # NOT for overriding course_title, course_description, or learning_objectives.
        course_config: dict = shared_state.get("course_config") or {}

        # Consistency check: log any mismatch between TO fields and extracted_inputs.
        _extracted_los = extracted.get("learning_objectives") or []
        _config_title = (course_config.get("course_title") or "").strip()
        _config_desc = (course_config.get("course_description") or "").strip()
        if _config_title and _config_title != course_title:
            logger.warning(
                "[A2][consistency] course_title mismatch — TO: %r | course_config: %r",
                course_title[:120],
                _config_title[:120],
            )
        if _config_desc and _config_desc != course_description:
            logger.warning(
                "[A2][consistency] description mismatch — TO: %d chars | course_config: %d chars",
                len(course_description),
                len(_config_desc),
            )
        if _extracted_los and _extracted_los != learning_objectives:
            logger.warning(
                "[A2][consistency] learning_objectives mismatch — TO: %d items | extracted: %d items",
                len(learning_objectives),
                len(_extracted_los),
            )
        logger.info(
            "[A2][consistency] Using TO as source of truth — title: %r | desc: %d chars | LOs: %d",
            course_title[:80],
            len(course_description),
            len(learning_objectives),
        )

        # Build source material guidance from per-file specs if present
        raw_specs: list[dict] = shared_state.get("source_file_specs") or []
        if raw_specs:
            # Sources with an extraction focus are listed first.
            sorted_specs = sorted(
                raw_specs,
                key=lambda s: (0 if (s.get("extract_hint") or "").strip() else 1),
            )
            guidance_lines = ["## Source Material Guidance"]
            guidance_lines.append(
                "The following source files were provided by the course author. "
                "Respect what they asked to get from each source when drawing on source material:"
            )
            for spec in sorted_specs:
                name = spec.get("blob_path", "").split("/")[-1]
                hint = (spec.get("extract_hint") or "").strip()
                if hint:
                    guidance_lines.append(f"- {name}: What to get from this source — {hint}")
                else:
                    guidance_lines.append(f"- {name}")
            source_guidance = "\n".join(guidance_lines)
            special_instructions = (
                f"{source_guidance}\n\n{special_instructions}"
                if special_instructions
                else source_guidance
            )

        # Resolve active rule pack from A0 classification
        rule_family = (
            shared_state.get("request_spec", {})
            .get("rule_classification", {})
            .get("family")
        )
        difficulty = (
            self.course_difficulty
            or shared_state.get("course_difficulty")
            or "intermediate"
        )
        rule_pack = (
            resolve_rule_pack(rule_family, difficulty) if rule_family else None
        )
        if not rule_pack:
            raise RuntimeError(
                f"Could not resolve rule pack for family '{rule_family}'"
            )

        total_subtopics = sum(len(l.get("subtopics", [])) for l in enriched_sections)
        logger.info("[A2] Course: %s", course_title)
        logger.info("[A2] Rule pack: %s %s", rule_pack["family"], rule_pack["version"])
        logger.info(
            "[A2] Difficulty: %s",
            rule_pack.get("active_difficulty", difficulty),
        )
        logger.info(
            "[A2] TO lessons: %s  |  subtopics to generate: %s",
            len(enriched_sections),
            total_subtopics,
        )
        logger.info("[A2] Learning objectives: %s", len(learning_objectives))

        # -- Step 2: Build multi-file chunk index (when source files available) --
        source_chunks: list[dict] | None = None
        effective_paths = self.source_file_paths
        if effective_paths is None:
            # Fallback: read from shared_state if caller didn't pass them directly
            raw_paths = shared_state.get("source_file_paths")
            if isinstance(raw_paths, list) and raw_paths:
                effective_paths = [p for p in raw_paths if isinstance(p, str)]

        if effective_paths and len(effective_paths) >= 1:
            try:
                from lectora_backend.pipeline.agent.a0_request_synthesizer.utils.chunker import (
                    chunk_files,
                )
                existing_paths = [p for p in effective_paths if Path(p).exists()]
                if len(existing_paths) >= 1:
                    source_chunks = chunk_files(existing_paths)
                    logger.info(
                        "[A2] Built %d chunks from %d source files for topic-wise retrieval.",
                        len(source_chunks),
                        len(existing_paths),
                    )
                else:
                    logger.debug(
                        "[A2] Only %d/%d source file(s) found on disk — skipping multi-file chunking.",
                        len(existing_paths),
                        len(effective_paths),
                    )
            except Exception as exc:
                logger.warning("[A2] Could not build chunk index: %s — proceeding without retrieval.", exc)

        # -- Step 3: Generate content section-by-section ----------------------
        if self.feedback:
            logger.info(
                "[A2] Generating content with prior S2 feedback applied (%s chars).",
                len(self.feedback),
            )
        else:
            logger.info("[A2] Generating content section-by-section...")
        generated_sections = generate_all_sections(
            enriched_sections=enriched_sections,
            docx_path=self.docx_path,
            learning_objectives=learning_objectives,
            rule_pack=rule_pack,
            feedback=self.feedback,
            source_chunks=source_chunks,
            shared_state_path=self.shared_state_path,
            audience=course_audience,
            special_instructions=special_instructions,
            course_config=course_config,
        )

        # -- Step 3b: Capstone exercise (after all lessons generated) ----------
        capstone = _build_capstone_exercise(
            course_title,
            learning_objectives,
            generated_sections,
            rule_pack,
        )
        if capstone:
            generated_sections.append(capstone)

        # -- Collect stats -----------------------------------------------------
        total_generated_words = sum(s.get("word_count", 0) for s in generated_sections)
        successful = sum(1 for s in generated_sections if s.get("status") == "generated")
        failed     = sum(1 for s in generated_sections if s.get("status") == "failed")
        skipped    = sum(1 for s in generated_sections if s.get("status") == "skipped_thin")

        logger.info("[A2] Generation complete:")
        logger.info(
            "     Sections: %s generated, %s skipped, %s failed",
            successful,
            skipped,
            failed,
        )
        logger.info("     Total words: %s", total_generated_words)

        stats = A2Stats(
            generated=successful,
            skipped=skipped,
            failed=failed,
            total_words=total_generated_words,
        )

        # -- Step 4: Course description (already resolved from llm_to_outline_classification above) --
        logger.info(
            "[A2] Course description: %d chars (from TO).",
            len(course_description),
        )
        course_conclusion = _build_course_conclusion(
            course_title,
            content_sample=content_sample,
            learning_objectives=learning_objectives,
            generated_sections=generated_sections,
        )

        # -- Step 5: Render styled .docx output (gated by self.render_docx) ---
        final_path: str | None = None
        if self.render_docx:
            logger.info("[A2] Rendering study guide .docx...")
            docx_output_path = self.output_dir / "study_guide.docx"
            final_path = build_study_guide_docx(
                course_title=course_title,
                course_description=course_description,
                learning_objectives=learning_objectives,
                generated_sections=generated_sections,
                output_path=str(docx_output_path),
                conclusion_text=course_conclusion,
            )
            logger.info("[A2] Study guide written -> %s", final_path)
        else:
            logger.info(
                "[A2] DOCX rendering deferred — will be built after S2 validation passes."
            )

        # -- Step 6: Build typed A2Output and persist -------------------------
        now = datetime.now(timezone.utc)
        content_json_path = self.output_dir / "generated_content.json"

        a2_result = A2Output(
            status="complete" if failed == 0 else "partial",
            run_id=run_id,
            course_title=course_title,
            sections=generated_sections,
            stats=stats,
            course_description=course_description,
            course_conclusion=course_conclusion,
            study_guide_docx=str(final_path) if final_path else None,
            generated_content_json=str(content_json_path),
            timestamp=now,
        )

        with open(content_json_path, "w") as f:
            json.dump(a2_result.model_dump(mode="json"), f, indent=2, default=str)

        # -- Step 7: Update shared state --------------------------------------
        logger.info("[A2] Updating shared state...")
        shared_state["agent_outputs"]["A2"] = a2_result.model_dump(mode="json")
        shared_state["status"] = "A2_complete"
        with open(self.shared_state_path, "w") as f:
            json.dump(shared_state, f, indent=2, default=str)

        logger.info("[A2] Done. Status: %s", a2_result.status)
        return a2_result


def render_study_guide_from_state(shared_state_path: str, output_dir: str = "") -> str:
    """
    Build `study_guide.docx` from a finalized shared_state.json.

    Intended to run AFTER S2 validation passes so that no docx is produced
    for content that fails compliance / structural checks.

    Args:
        shared_state_path: Path to the run's shared_state.json with A2 output present.
        output_dir:        Override directory for the docx; defaults to the shared
                           state's parent directory.

    Returns:
        Absolute path of the rendered DOCX file.
    """
    with open(shared_state_path) as f:
        shared_state = json.load(f)

    a2_output = shared_state.get("agent_outputs", {}).get("A2") or {}
    if not a2_output:
        raise RuntimeError("Cannot render study guide: A2 output is missing from shared state.")

    sections = a2_output.get("sections") or []
    if not sections:
        raise RuntimeError("Cannot render study guide: A2 produced no sections.")

    course_title = a2_output.get("course_title") or "Untitled Course"
    extracted = shared_state.get("extracted_inputs", {}) or {}
    learning_objectives = extracted.get("learning_objectives", []) or []
    content_sample = extracted.get("content_sample") or ""
    # Use the description stored during A2 run (sourced from llm_to_outline_classification).
    course_description = (a2_output.get("course_description") or "").strip()

    course_conclusion = (a2_output.get("course_conclusion") or "").strip()
    if not course_conclusion:
        course_conclusion = _build_course_conclusion(
            course_title,
            content_sample=content_sample,
            learning_objectives=learning_objectives,
            generated_sections=sections,
        )

    target_dir = Path(output_dir) if output_dir else Path(shared_state_path).expanduser().resolve().parent
    docx_output_path = target_dir / "study_guide.docx"

    final_path = build_study_guide_docx(
        course_title=course_title,
        course_description=course_description,
        learning_objectives=learning_objectives,
        generated_sections=sections,
        output_path=str(docx_output_path),
        conclusion_text=course_conclusion,
    )

    a2_output["study_guide_docx"] = str(final_path)
    shared_state["agent_outputs"]["A2"] = a2_output
    shared_state["status"] = "study_guide_rendered"
    with open(shared_state_path, "w") as f:
        json.dump(shared_state, f, indent=2, default=str)

    logger.info("[A2] Study guide written (post-S2) -> %s", final_path)
    return final_path
