"""Health-check endpoint."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def health() -> dict:
    return {"status": "ok"}
