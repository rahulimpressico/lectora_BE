"""Microsoft Entra ID token validation helpers."""
from __future__ import annotations

import threading
import time
from typing import Any

import requests
from fastapi import HTTPException, status
from jose import JWTError, jwt

from lectora_backend.config import settings


JWKS_CACHE_TTL_SECONDS = 3600


class EntraTokenValidator:
    def __init__(self) -> None:
        self._openid_config: dict[str, Any] | None = None
        self._openid_config_loaded_at = 0.0
        self._jwks: dict[str, Any] | None = None
        self._jwks_loaded_at = 0.0
        self._lock = threading.Lock()

    def _require_settings(self) -> None:
        missing = [
            name
            for name, value in (
                ("AZURE_TENANT_ID", settings.azure_tenant_id),
                ("AZURE_CLIENT_ID", settings.azure_client_id),
            )
            if not value
        ]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Missing Entra auth configuration: {', '.join(missing)}",
            )

    def _authority(self) -> str:
        self._require_settings()
        return f"https://login.microsoftonline.com/{settings.azure_tenant_id}/v2.0"

    def _openid_config_url(self) -> str:
        return f"{self._authority()}/.well-known/openid-configuration"

    def _load_openid_config(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if self._openid_config is not None and (
                now - self._openid_config_loaded_at < JWKS_CACHE_TTL_SECONDS
            ):
                return self._openid_config

        try:
            response = requests.get(self._openid_config_url(), timeout=5)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unable to load Entra OpenID configuration — see server logs.",
            ) from exc

        with self._lock:
            self._openid_config = response.json()
            self._openid_config_loaded_at = now
        return self._openid_config

    def _load_jwks(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if self._jwks is not None and now - self._jwks_loaded_at < JWKS_CACHE_TTL_SECONDS:
                return self._jwks

        openid_config = self._load_openid_config()
        jwks_uri = openid_config.get("jwks_uri")
        if not jwks_uri:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Entra OpenID configuration did not provide jwks_uri.",
            )

        try:
            response = requests.get(jwks_uri, timeout=5)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unable to load Entra signing keys — see server logs.",
            ) from exc

        with self._lock:
            self._jwks = response.json()
            self._jwks_loaded_at = now
        return self._jwks

    def _find_signing_key(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token header.",
            ) from exc

        kid = header.get("kid")
        if not kid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bearer token is missing key identifier.",
            )

        jwks = self._load_jwks()
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return key

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Signing key not found for bearer token.",
        )

    def validate_token(self, token: str) -> dict[str, Any]:
        signing_key = self._find_signing_key(token)
        openid_config = self._load_openid_config()
        issuer = openid_config.get("issuer") or self._authority()

        audiences = [value for value in (settings.azure_audience, settings.azure_client_id) if value]

        try:
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=audiences,
                issuer=issuer,
                options={"verify_at_hash": False},
            )
        except JWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bearer token validation failed.",
            ) from exc

        return claims
