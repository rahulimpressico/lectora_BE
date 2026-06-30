from .base import *

def _missing_input_response(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": {
                "code": "MISSING_REQUIRED_INPUT",
                "message": message,
                "stage": None,
                "retryable": False,
            }
        },
    )

def _job_init_error_response(message: str, retryable: bool) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "JOB_INITIALIZATION_FAILED",
                "message": message,
                "stage": None,
                "retryable": retryable,
            }
        },
    )

def _map_job_error(error_detail: str | None) -> JobErrorDetail | None:
    if not error_detail:
        return None

    fallback = JobErrorDetail(
        code="MALFORMED_ERROR_DETAIL",
        message=error_detail,
        stage=None,
        retryable=False,
    )

    try:
        payload = json.loads(error_detail)
    except json.JSONDecodeError:
        return fallback

    if not isinstance(payload, dict):
        return fallback

    stage = payload.get("stage")
    try:
        parsed_stage = PipelineStep(stage) if stage else None
    except ValueError:
        parsed_stage = None

    return JobErrorDetail(
        code=str(payload.get("code") or fallback.code),
        message=str(payload.get("message") or fallback.message),
        stage=parsed_stage,
        retryable=bool(payload.get("retryable", fallback.retryable)),
    )

def _map_job_detail(job) -> JobDetailResponse:
    # Use precomputed O(1) lookup instead of PIPELINE_ORDER.index() which raises
    # ValueError on unknown stages and is O(n) per call.
    ordered_stage_progress = sorted(
        job.stage_progress,
        key=lambda item: STAGE_ORDER.get(item.stage_id, len(PIPELINE_ORDER)),
    )

    return JobDetailResponse(
        job_id=job.job_id,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        stages=[
            StageProgressResponse(
                stage=stage.stage_id,
                status=stage.status,
                started_at=stage.started_at,
                completed_at=stage.completed_at,
                outcome=ValidationOutcome(stage.validation_outcome.value)
                if stage.validation_outcome
                else None,
            )
            for stage in ordered_stage_progress
        ],
        error=_map_job_error(
            next(
                (
                    stage.error_detail
                    for stage in ordered_stage_progress
                    if stage.error_detail
                ),
                None,
            )
        ),
    )

def _map_artifacts(job) -> list[ArtifactSummary]:
    state = StateManager().load(job.job_id, blob_path=job.shared_state_blob_path)
    artifact_refs = state.get("artifactRefs", {})
    created_at = job.updated_at or job.created_at
    artifacts: list[ArtifactSummary] = []

    for artifact_type, value in artifact_refs.items():
        stage = _ARTIFACT_STAGE_MAP.get(artifact_type, "")

        if isinstance(value, dict) and value.get("blobPath"):
            artifacts.append(
                ArtifactSummary(
                    type=artifact_type,
                    blob_path=str(value["blobPath"]),
                    stage=stage,
                    is_latest=True,
                    created_at=created_at,
                )
            )
            continue

        if artifact_type == "images" and isinstance(value, list):
            for item in value:
                blob_path = item.get("blobPath")
                if not blob_path:
                    continue
                image_name = item.get("fileName") or artifact_type
                artifacts.append(
                    ArtifactSummary(
                        type=f"image:{image_name}",
                        blob_path=str(blob_path),
                        stage=stage,
                        is_latest=True,
                        created_at=created_at,
                    )
                )

    return artifacts
