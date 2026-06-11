"""Blob storage persistence for shared state and artifacts."""
import threading
import time

from azure.core.exceptions import (
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from azure.storage.blob import BlobServiceClient, ContentSettings

from lectora_backend.config import settings

# Per-process cache of container names that have already been verified to exist.
# Eliminates the Azure API round-trip on every BlobRepository instantiation.
_verified_containers: set[str] = set()
_verified_lock = threading.Lock()  # guards the TOCTOU check-and-add sequence

_UPLOAD_RETRIES = 3
_UPLOAD_RETRY_DELAY = 1.0


def _upload_with_retry(upload_fn, *args, **kwargs) -> None:
    """Retry an upload callable on transient Azure errors."""
    last_exc: Exception | None = None
    for attempt in range(1, _UPLOAD_RETRIES + 1):
        try:
            upload_fn(*args, **kwargs)
            return
        except (ServiceResponseTimeoutError, ServiceResponseError) as exc:
            last_exc = exc
            if attempt < _UPLOAD_RETRIES:
                time.sleep(_UPLOAD_RETRY_DELAY * attempt)
    raise last_exc  # type: ignore[misc]


class BlobRepository:
    _DOWNLOAD_RETRIES = 3
    _UPLOAD_BLOCK_SIZE = 64 * 1024
    _DELETE_BATCH_SIZE = 256  # Azure batch-delete limit per request

    def __init__(self, container_name: str | None = None) -> None:
        self._service_client = BlobServiceClient.from_connection_string(
            settings.azure_storage_connection_string,
            connection_timeout=10,
            read_timeout=30,
            max_single_put_size=self._UPLOAD_BLOCK_SIZE,
            max_block_size=self._UPLOAD_BLOCK_SIZE,
        )
        self._container_name = container_name or settings.blob_container_name
        self._container_client = self._service_client.get_container_client(
            self._container_name
        )

        # Only check/create once per container name per process lifetime.
        # Lock prevents TOCTOU: two threads simultaneously seeing the container absent.
        with _verified_lock:
            if self._container_name not in _verified_containers:
                if not self._container_client.exists():
                    try:
                        self._container_client.create_container()
                    except ResourceExistsError:
                        pass  # already created by another thread/process
                _verified_containers.add(self._container_name)

    def upload_text(self, blob_path: str, content: str) -> None:
        blob_client = self._container_client.get_blob_client(blob_path)
        _upload_with_retry(blob_client.upload_blob, content, overwrite=True)

    def download_bytes(self, blob_path: str) -> bytes:
        blob_client = self._container_client.get_blob_client(blob_path)
        for attempt in range(1, self._DOWNLOAD_RETRIES + 1):
            try:
                return blob_client.download_blob().readall()
            except ResourceNotFoundError:
                raise FileNotFoundError(
                    f"Blob not found in storage: {blob_path!r}"
                ) from None
            except (ServiceResponseTimeoutError, ServiceResponseError):
                if attempt == self._DOWNLOAD_RETRIES:
                    raise
                time.sleep(0.5 * attempt)

    def upload_file(
        self,
        local_path: str,
        blob_path: str,
        content_type: str | None = None,
    ) -> None:
        with open(local_path, "rb") as f:
            self.upload_bytes(
                blob_path=blob_path,
                content=f.read(),
                content_type=content_type,
            )

    def download_text(self, blob_path: str) -> str:
        return self.download_bytes(blob_path).decode("utf-8")

    def upload_bytes(
        self,
        blob_path: str,
        content: bytes,
        content_type: str | None = None,
    ) -> None:
        blob_client = self._container_client.get_blob_client(blob_path)
        if content_type:
            _upload_with_retry(
                blob_client.upload_blob,
                content,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )
        else:
            _upload_with_retry(blob_client.upload_blob, content, overwrite=True)

    def exists(self, blob_path: str) -> bool:
        return self._container_client.get_blob_client(blob_path).exists()

    def delete_blob(self, blob_path: str) -> None:
        """Idempotent delete — silently ignores already-deleted blobs."""
        blob_client = self._container_client.get_blob_client(blob_path)
        try:
            blob_client.delete_blob()
        except ResourceNotFoundError:
            pass  # already deleted — idempotent

    def list_blobs(self, prefix: str) -> list[str]:
        return [
            blob.name
            for blob in self._container_client.list_blobs(name_starts_with=prefix)
        ]

    def list_prefixes(self, prefix: str = "") -> list[str]:
        """Return virtual directory names one level below *prefix*."""
        normalized = prefix.strip().lstrip("/")
        start = f"{normalized}/" if normalized else ""
        prefixes: list[str] = []
        for item in self._container_client.walk_blobs(
            name_starts_with=start,
            delimiter="/",
        ):
            name = getattr(item, "name", None)
            if not name or not name.endswith("/"):
                continue
            rel = name[len(start) :].strip("/")
            if rel and "/" not in rel:
                prefixes.append(rel)
        return prefixes

    def delete_blobs_by_prefix(self, prefix: str) -> int:
        """Delete all blobs whose names start with *prefix*.

        Uses Azure batch-delete (up to 256 blobs per HTTP request) instead of
        serial single-blob deletes, reducing Azure round-trips significantly.
        Returns the count of blobs submitted for deletion.
        """
        normalized = prefix.strip().lstrip("/")
        if not normalized:
            return 0
        if not normalized.endswith("/"):
            normalized = f"{normalized}/"

        blob_names = self.list_blobs(normalized)
        if not blob_names:
            return 0

        removed = 0
        for i in range(0, len(blob_names), self._DELETE_BATCH_SIZE):
            batch = blob_names[i : i + self._DELETE_BATCH_SIZE]
            # raise_on_any_failure=False: continue even if a blob was already deleted
            self._container_client.delete_blobs(*batch, raise_on_any_failure=False)
            removed += len(batch)

        return removed

    def prefix_exists(self, prefix: str) -> bool:
        normalized = prefix.rstrip("/") + "/"
        iterator = self._container_client.list_blobs(name_starts_with=normalized)
        return next(iter(iterator), None) is not None
