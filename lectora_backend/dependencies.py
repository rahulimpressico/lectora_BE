"""FastAPI dependency injection: DB session and current user."""
from collections.abc import Generator
from functools import lru_cache

from fastapi import Depends
from fastapi.security import OAuth2AuthorizationCodeBearer
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from lectora_backend.api.middleware.auth import EntraTokenValidator
from lectora_backend.config import settings

# ── Database engine ────────────────────────────────────────────────────────────

if settings.database_url.startswith("sqlite"):
    # SQLite: use StaticPool so the same in-memory/file connection is reused
    # across threads, required because SQLite does not support multiple concurrent
    # writers. check_same_thread=False lets SQLAlchemy route requests from any
    # thread through the single connection.
    engine = create_engine(
        settings.database_url,
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    # Production databases (Postgres / Azure SQL): configure a bounded pool so
    # we never exhaust server-side connections under concurrent load.
    engine = create_engine(
        settings.database_url,
        echo=False,
        pool_size=10,       # resident connections kept open
        max_overflow=20,    # burst headroom on top of pool_size
        pool_timeout=30,    # seconds to wait for a free connection
        pool_pre_ping=True, # discard stale connections before handing them out
    )

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


# ── Auth ───────────────────────────────────────────────────────────────────────

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
