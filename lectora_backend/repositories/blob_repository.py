"""Blob storage persistence for shared state and artifacts."""
import time

from azure.core.exceptions import (
    ResourceNotFoundError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from azure.storage.blob import BlobServiceClient, ContentSettings

from lectora_backend.config import settings


class BlobRepository:
    _DOWNLOAD_RETRIES = 3
    _UPLOAD_BLOCK_SIZE = 64 * 1024

    def __init__(self, container_name: str | None = None) -> None:
        self._service_client = BlobServiceClient.from_connection_string(
            settings.azure_storage_connection_string,
            connection_timeout=5,
            read_timeout=5,
            max_single_put_size=self._UPLOAD_BLOCK_SIZE,
            max_block_size=self._UPLOAD_BLOCK_SIZE,
        )
        resolved = container_name or settings.blob_container_name
        self._container_name = resolved
        self._container_client = self._service_client.get_container_client(resolved)

        if not self._container_client.exists():
            self._container_client.create_container()

    def upload_text(self, blob_path: str, content: str) -> None:
        blob_client = self._container_client.get_blob_client(blob_path)
        blob_client.upload_blob(content, overwrite=True)

    def download_bytes(self, blob_path: str) -> bytes:
        blob_client = self._container_client.get_blob_client(blob_path)
        for attempt in range(1, self._DOWNLOAD_RETRIES + 1):
            try:
                return blob_client.download_blob(max_concurrency=1).readall()
            except ResourceNotFoundError:
                # Blob does not exist — no point retrying.
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
            return

        blob_client.upload_blob(content, overwrite=True)

    def exists(self, blob_path: str) -> bool:
        blob_client = self._container_client.get_blob_client(blob_path)
        return blob_client.exists()

    def delete_blob(self, blob_path: str) -> None:
        blob_client = self._container_client.get_blob_client(blob_path)
        if blob_client.exists():
            blob_client.delete_blob()

    def list_blobs(self, prefix: str) -> list[str]:
        return [blob.name for blob in self._container_client.list_blobs(name_starts_with=prefix)]

    def delete_blobs_by_prefix(self, prefix: str) -> int:
        """Delete all blobs whose names start with *prefix*. Returns count deleted."""
        normalized = prefix.strip().lstrip("/")
        if not normalized:
            return 0
        if not normalized.endswith("/"):
            normalized = f"{normalized}/"
        removed = 0
        for name in self.list_blobs(normalized):
            self.delete_blob(name)
            removed += 1
        return removed

    def prefix_exists(self, prefix: str) -> bool:
        normalized = prefix.rstrip("/") + "/"
        iterator = self._container_client.list_blobs(name_starts_with=normalized)
        return next(iter(iterator), None) is not None
