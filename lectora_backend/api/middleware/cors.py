"""CORS for browser clients (local Vite, Docker FE, etc.)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from lectora_backend.config import settings


def add_cors_middleware(app: FastAPI) -> None:
    regex = (settings.cors_origin_regex or "").strip() or None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list(),
        allow_origin_regex=regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )
