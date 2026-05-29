"""Pydantic settings loaded from environment / .env file."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Resolve `.env` deterministically from repo root so agents/pipeline/worker
    # behave the same regardless of current working directory.
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[1] / ".env"),
        extra="ignore",
    )

    app_name: str = "Lectora Backend"
    app_version: str = "0.1.0"

    # Database
    database_url: str = "sqlite:///./lectora.db"

    # Azure Service Bus
    service_bus_namespace: str = ""
    service_bus_connection_string: str = ""
    queue_name: str = "course-jobs"

    # Azure OpenAI
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = ""

    # Microsoft Entra ID
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_audience: str = ""

    # Azure Blob Storage
    azure_storage_connection_string: str = ""
    blob_container_name: str = "regedlectoraaistorage"
    # Container for FE-uploaded source documents (Documents library).
    uploaded_documents_container_name: str = "uploaded-documents"

    # Langfuse
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # Comma-separated browser origins (FE dev server, Docker FE on :8080, Netlify, etc.)
    cors_origins: str = (
        "http://localhost:5173,"
        "http://127.0.0.1:5173,"
        "http://localhost:3000,"
        "http://localhost:8080,"
        "https://nimble-cendol-69a81c.netlify.app"
    )

    # Extra origins matched by regex (Netlify branch/preview deploys).
    cors_origin_regex: str = r"https://.*\.netlify\.app"

    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def is_azure_storage_configured(self) -> bool:
        """True when Azure Blob Storage connection string is present."""
        return bool(self.azure_storage_connection_string.strip())


settings = Settings()
