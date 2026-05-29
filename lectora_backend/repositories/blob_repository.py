"""Blob storage persistence for shared state and artifacts."""
import time

from azure.core.exceptions import (
    ResourceNotFoundError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from azure.storage.blob import BlobServiceClient, ContentSettings

from lectora_backend.config import settings

# Per-process cache of container names that have already been verified to exist.
# Eliminates the Azure API round-trip on every BlobRepository instantiation.
_verified_containers: set[str] = set()


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
        if self._container_name not in _verified_containers:
            if not self._container_client.exists():
                self._container_client.create_container()
            _verified_containers.add(self._container_name)

    def upload_text(self, blob_path: str, content: str) -> None:
        blob_client = self._container_client.get_blob_client(blob_path)
        blob_client.upload_blob(content, overwrite=True)

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
            blob_client.upload_blob(
                content,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )
        else:
            blob_client.upload_blob(content, overwrite=True)

    def exists(self, blob_path: str) -> bool:
        return self._container_client.get_blob_client(blob_path).exists()

    def delete_blob(self, blob_path: str) -> None:
        blob_client = self._container_client.get_blob_client(blob_path)
        if blob_client.exists():
            blob_client.delete_blob()

    def list_blobs(self, prefix: str) -> list[str]:
        return [
            blob.name
            for blob in self._container_client.list_blobs(name_starts_with=prefix)
        ]

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
