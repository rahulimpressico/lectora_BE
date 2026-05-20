"""FastAPI dependency injection: DB session and current user."""
from collections.abc import Generator
from functools import lru_cache

from fastapi import Depends
from fastapi.security import OAuth2AuthorizationCodeBearer
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from lectora_backend.api.middleware.auth import EntraTokenValidator
from lectora_backend.config import settings


connect_args = {"check_same_thread": False} if settings.database_url.startswith(
    "sqlite") else {}

engine = create_engine(settings.database_url, echo=False,
                       connect_args=connect_args)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl=(
        f"https://login.microsoftonline.com/{settings.azure_tenant_id or 'common'}/oauth2/v2.0/authorize"
    ),
    tokenUrl=f"https://login.microsoftonline.com/{settings.azure_tenant_id or 'common'}/oauth2/v2.0/token",
)


@lru_cache(maxsize=1)
def get_entra_token_validator() -> EntraTokenValidator:
    return EntraTokenValidator()


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    validator: EntraTokenValidator = Depends(get_entra_token_validator),
) -> dict:
    return validator.validate_token(token)
