"""SQLAlchemy database setup — synchronous + asynchronous, lazily created.

This module exposes **two parallel engines** against the same Postgres
database:

* ``_get_engine`` / ``_get_session_factory`` / :func:`get_db`
    The synchronous path used by every Celery worker, every service in
    ``app.services``, and the FastAPI routes that already exist.
    Synchronous SQLAlchemy is the right call for code that mixes
    transactional work with synchronous LLM SDK calls (e.g. the Groq
    client) — pretending those calls are async would just bury a thread
    pool inside the framework.

* ``_get_async_engine`` / ``_get_async_session_factory`` /
  :func:`get_async_db`
    The async path for high-throughput, IO-bound endpoints (notifications
    inbox, audit query, anything that reads but doesn't call the LLM).
    Backed by ``asyncpg``.  Endpoints opt-in by depending on
    :func:`get_async_db` instead of :func:`get_db`.

Both paths share the same :data:`Base` and ORM classes, so an ``async``
endpoint can be added next to a sync one without any model changes.

Why not migrate everything?
---------------------------
The audit flagged async support as a scalability gap.  Forcing every
service / worker / Celery task to ``async def`` would touch 70+ files
and break interactions with the synchronous Groq + httpx SDKs without
buying us anything — the bottleneck on the LLM-heavy paths is the
model, not Python.  The hybrid model keeps sync paths fast where they
already are and gives async paths room to grow where the gain matters.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Column, DateTime, create_engine
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, declarative_base, declared_attr, sessionmaker

# ── Module-level singletons (lazily populated) ────────────────────────
_engine: Any | None = None
_SessionLocal: sessionmaker[Session] | None = None

# Async counterparts.
_async_engine: Optional[AsyncEngine] = None
_AsyncSessionLocal: Optional[async_sessionmaker[AsyncSession]] = None


class TimestampMixin:
    """Mixin that adds ``created_at`` and ``updated_at`` UTC timestamp columns.

    ``updated_at`` is refreshed automatically on every ``UPDATE`` via
    SQLAlchemy's ``onupdate`` hook.

    The methods deliberately have no return-type annotation: under SQLAlchemy
    2.0's annotated-declarative scanner, ``Column[datetime]`` is interpreted
    as a mapping directive and triggers ``ArgumentError`` unless wrapped in
    ``Mapped[]``. Leaving the annotation off keeps the mixin classic-style
    and compatible with every model that inherits from ``Base``.
    """

    @declared_attr
    def created_at(cls):  # noqa: N805, ANN201
        return Column(
            DateTime(timezone=True),
            default=lambda: datetime.now(timezone.utc),
            nullable=False,
        )

    @declared_attr
    def updated_at(cls):  # noqa: N805, ANN201
        return Column(
            DateTime(timezone=True),
            default=lambda: datetime.now(timezone.utc),
            onupdate=lambda: datetime.now(timezone.utc),
            nullable=False,
        )


# Declarative base that every model should inherit from.
# Models also inherit TimestampMixin so that created_at / updated_at
# are present on every table automatically.
Base = declarative_base(cls=TimestampMixin)

# Convenience re-export so models can do:
#   from app.database import PGUUID
PGUUID = UUID(as_uuid=True)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _to_async_url(url: str) -> str:
    """Translate a sync Postgres URL to its asyncpg-backed counterpart.

    Accepts ``postgresql://`` / ``postgresql+psycopg2://`` and rewrites
    the driver name to ``postgresql+asyncpg``.  No-op for URLs that are
    already async-shaped.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg2://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    # Unknown scheme — return unchanged and let SQLAlchemy raise a
    # clearer error than we could.
    return url


# ---------------------------------------------------------------------------
# Synchronous engine + session
# ---------------------------------------------------------------------------

def _get_engine() -> Any:
    """Return the synchronous SQLAlchemy engine, creating it on first call."""
    global _engine  # noqa: PLW0603
    if _engine is None:
        from app.config import get_settings

        settings = get_settings()
        _engine = create_engine(
            settings.DATABASE_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            echo=settings.SQL_ECHO,
        )
    return _engine


def _get_session_factory() -> sessionmaker[Session]:
    """Return the sync session factory, creating it on first call."""
    global _SessionLocal  # noqa: PLW0603
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=_get_engine(),
        )
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a synchronous database session.

    The session is automatically closed when the request finishes,
    regardless of whether an exception occurred.

    Example::

        @router.get("/items")
        def list_items(db: Session = Depends(get_db)):
            return db.query(Item).all()
    """
    session_factory = _get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Asynchronous engine + session
# ---------------------------------------------------------------------------

def _get_async_engine() -> AsyncEngine:
    """Return the asyncpg-backed engine, creating it on first call.

    Pool sizing mirrors the sync path so we don't accidentally double
    the connection footprint when async + sync paths run side-by-side
    against the same database.
    """
    global _async_engine  # noqa: PLW0603
    if _async_engine is None:
        from app.config import get_settings

        settings = get_settings()
        _async_engine = create_async_engine(
            _to_async_url(settings.DATABASE_URL),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            echo=settings.SQL_ECHO,
        )
    return _async_engine


def _get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the async session factory, creating it on first call."""
    global _AsyncSessionLocal  # noqa: PLW0603
    if _AsyncSessionLocal is None:
        _AsyncSessionLocal = async_sessionmaker(
            bind=_get_async_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
            autoflush=False,
        )
    return _AsyncSessionLocal


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an :class:`AsyncSession`.

    Mirror of :func:`get_db` for endpoints that want to ride the
    asyncpg path.  Endpoints must ``await`` every ORM call when using
    this dependency.

    Example::

        @router.get("/things")
        async def list_things(db: AsyncSession = Depends(get_async_db)):
            result = await db.execute(select(Thing))
            return result.scalars().all()
    """
    session_factory = _get_async_session_factory()
    async with session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Test / shutdown helpers
# ---------------------------------------------------------------------------

async def dispose_async_engine() -> None:
    """Close the async pool — call from app shutdown / test teardown."""
    global _async_engine  # noqa: PLW0603
    if _async_engine is not None:
        await _async_engine.dispose()
        _async_engine = None


def dispose_sync_engine() -> None:
    """Close the sync pool — call from app shutdown / test teardown."""
    global _engine, _SessionLocal  # noqa: PLW0603
    if _engine is not None:
        _engine.dispose()
        _engine = None
        _SessionLocal = None
