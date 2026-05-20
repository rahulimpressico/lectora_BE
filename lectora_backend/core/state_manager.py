
import json
from typing import Any

from sqlalchemy import select

from lectora_backend.dependencies import SessionLocal
from lectora_backend.models.db_models import Job
from lectora_backend.repositories.blob_repository import BlobRepository

class StateManager:
    def __init__(self, blob_repository: BlobRepository | None = None) -> None:
        self._blob_repository = blob_repository or BlobRepository()

    def _legacy_state_blob_path(self, job_id: str) -> str:
        return f"state/{job_id}/shared_state.json"

    def _resolve_blob_path(self, job_id: str, blob_path: str | None = None) -> str:
        if blob_path:
            return blob_path

        session = SessionLocal()
        try:
            stored_path = session.execute(
                select(Job.shared_state_blob_path).where(Job.job_id == job_id)
            ).scalar_one_or_none()
        finally:
            session.close()

        return stored_path or self._legacy_state_blob_path(job_id)

    def initialize(
        self,
        job_id: str,
        initial_state: dict[str, Any],
        *,
        blob_path: str | None = None,
    ) -> None:
        blob_path = self._resolve_blob_path(job_id, blob_path)
        self._blob_repository.upload_text(
            blob_path=blob_path,
            content=json.dumps(initial_state, indent=2),
        )


    def load(self, job_id:str, *, blob_path: str | None = None)-> dict[str,Any]:
        blob_path = self._resolve_blob_path(job_id, blob_path)
        content = self._blob_repository.download_text(blob_path)
        return json.loads(content)

    def save(
        self,
        job_id: str,
        state: dict[str, Any],
        *,
        blob_path: str | None = None,
    ) -> None:
        blob_path = self._resolve_blob_path(job_id, blob_path)
        self._blob_repository.upload_text(
            blob_path=blob_path,
            content=json.dumps(state, indent=2),
        )

    def exists(self, job_id: str, *, blob_path: str | None = None) -> bool:
        blob_path = self._resolve_blob_path(job_id, blob_path)
        return self._blob_repository.exists(blob_path)

    def delete(self, job_id: str, *, blob_path: str | None = None) -> None:
        blob_path = self._resolve_blob_path(job_id, blob_path)
        self._blob_repository.delete_blob(blob_path)
