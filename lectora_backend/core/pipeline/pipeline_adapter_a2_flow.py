import logging
from pathlib import Path
from typing import Any

from lectora_backend.models.constants import MAX_A2_S2_CYCLES
from lectora_backend.pipeline.agent.a2_content_generator.main import (
    A2ContentGenerator,
    render_study_guide_from_state,
)
from lectora_backend.pipeline.agent.kc_planner.main import run as kc_planner_run
from lectora_backend.pipeline.agent.s2_validator.main import S2Validator
from lectora_backend.pipeline.agent.section_mapper.main import run as section_mapper_run
from lectora_backend.pipeline.models.validation import S2Status
from lectora_backend.pipeline.shared_utils.validation_helpers import format_s2_feedback

logger = logging.getLogger(__name__)


def run_a2_flow(
    adapter: Any,
    job_id: str,
    *,
    state_blob_path: str,
    pipeline_shared_state_path: str,
    study_guide_path: str,
    course_difficulty: str = "intermediate",
    extra_source_blob_paths: list[str] | None = None,
) -> dict[str, Any]:
    # Download extra source files for multi-file chunk retrieval.
    source_file_paths: list[str] | None = None
    if extra_source_blob_paths:
        temp_dir = Path(pipeline_shared_state_path).parent / "_source_chunks"
        temp_dir.mkdir(exist_ok=True)
        downloaded: list[str] = [study_guide_path]
        for blob_path in extra_source_blob_paths:
            try:
                local_path = temp_dir / Path(blob_path).name
                if not local_path.exists():
                    content = adapter._blob_repository.download_bytes(blob_path)
                    local_path.write_bytes(content)
                downloaded.append(str(local_path))
            except Exception as exc:
                logger.warning(
                    "Could not download extra source file %s for A2 retrieval: %s",
                    blob_path,
                    exc,
                )
        if len(downloaded) > 1:
            source_file_paths = downloaded
            logger.info(
                "[run_a2] %d source files available for chunk-based retrieval.",
                len(source_file_paths),
            )

    section_map_result = section_mapper_run(shared_state_path=pipeline_shared_state_path)
    enriched_sections_path = Path(pipeline_shared_state_path).parent / "enriched_sections.json"

    kc_result = kc_planner_run(shared_state_path=pipeline_shared_state_path)
    logger.info(
        "KC Planner: scenario=%s kc_count=%s",
        kc_result.get("scenario"),
        kc_result.get("kc_count"),
    )

    a2_result: Any = None
    s2_result: Any = None
    a2_feedback: str | None = None

    for cycle in range(1, MAX_A2_S2_CYCLES + 1):
        logger.info("A2/S2 cycle %s/%s", cycle, MAX_A2_S2_CYCLES)

        a2_result = A2ContentGenerator(
            shared_state_path=pipeline_shared_state_path,
            docx_path=study_guide_path,
            render_docx=False,
            course_difficulty=course_difficulty,
            feedback=a2_feedback,
            source_file_paths=source_file_paths,
        ).run()
        logger.info(
            "A2 status=%s generated=%s skipped=%s failed=%s words=%s",
            a2_result.status,
            a2_result.stats.generated,
            a2_result.stats.skipped,
            a2_result.stats.failed,
            a2_result.stats.total_words,
        )

        s2_result = S2Validator(pipeline_shared_state_path).run()
        logger.info(
            "S2 status=%s blockers=%s warnings=%s",
            s2_result.status,
            s2_result.blockers,
            s2_result.warnings,
        )

        if s2_result.status not in (S2Status.blocked, S2Status.blocker):
            logger.info("S2 passed — content cleared for DOCX rendering.")
            break

        if cycle < MAX_A2_S2_CYCLES:
            logger.warning(
                "S2 blocked (cycle %s/%s) — regenerating A2 with feedback.",
                cycle,
                MAX_A2_S2_CYCLES,
            )
            a2_feedback = format_s2_feedback(s2_result)

    s2_hard_blocked = bool(s2_result) and s2_result.status in (
        S2Status.blocked,
        S2Status.blocker,
    )

    final_docx_path: str | None = None
    if not s2_hard_blocked:
        final_docx_path = render_study_guide_from_state(
            shared_state_path=pipeline_shared_state_path
        )
        logger.info("Study guide rendered -> %s", final_docx_path)
    else:
        logger.error(
            "S2 still blocked after %s cycle(s) — study_guide.docx NOT built.",
            MAX_A2_S2_CYCLES,
        )

    artifact_refs = adapter._persist_a2_outputs(
        job_id=job_id,
        state_blob_path=state_blob_path,
        pipeline_shared_state_path=pipeline_shared_state_path,
        enriched_sections_path=enriched_sections_path,
        a2_result=a2_result,
        final_docx_path=final_docx_path,
    )
    if s2_result:
        adapter._persist_s2_outputs(
            job_id=job_id,
            state_blob_path=state_blob_path,
            pipeline_shared_state_path=pipeline_shared_state_path,
            s2_result=s2_result,
        )

    return {
        "sectionMap": section_map_result,
        "a2": a2_result,
        "s2": s2_result,
        "s2_hard_blocked": s2_hard_blocked,
        "artifactRefs": artifact_refs,
    }

