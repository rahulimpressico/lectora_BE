"""Health-check endpoints."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/search")
async def health_search() -> dict:
    """
    Report Azure AI Search connectivity and index status.

    Use this to verify the configured index exists and is queryable.
  """
    from lectora_backend.config import settings

    endpoint = (settings.azure_search_endpoint or "").rstrip("/")
    index_name = settings.azure_search_index_name or "course-chunks"
    if not endpoint or not settings.azure_search_api_key:
        return {
            "configured": False,
            "endpoint": endpoint or None,
            "index_name": index_name,
            "message": "Set AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_API_KEY in .env",
        }

    from lectora_backend.ingestion.storage.azure_search_client import AzureSearchIngestionClient

    client = AzureSearchIngestionClient(
        endpoint=endpoint,
        api_key=settings.azure_search_api_key,
        index_name=index_name,
    )
    return client.describe_index()
