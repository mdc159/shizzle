"""Async database engine/session plumbing.

Schema authority is Alembic (server/alembic) for Postgres. For the SQLite
fallback (single-container `local` profile) `init_db` creates tables directly —
SQLite there is a throwaway per-machine cache, not a migrated production store.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    kwargs: dict = {"echo": echo}
    if database_url.startswith("postgresql"):
        kwargs["pool_pre_ping"] = True
    return create_async_engine(database_url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create tables when running on SQLite (local profile). Postgres uses Alembic."""
    if engine.dialect.name == "sqlite":
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


__all__ = [
    "AsyncEngine",
    "AsyncSession",
    "Base",
    "async_sessionmaker",
    "create_engine",
    "create_session_factory",
    "init_db",
]
