"""Async database engine/session plumbing.

Schema authority is Alembic (library/alembic) for Postgres. For the SQLite
fallback (single-container `local` profile) `init_db` creates tables directly —
SQLite there is a throwaway per-machine cache, not a migrated production store.
"""

from __future__ import annotations

from sqlalchemy import event
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
    if database_url.startswith("sqlite"):
        # Several writers (orchestrator, API, tests' scripted nodes) share one
        # file. With SQLite's default deferred transactions a session that
        # read first and writes later must upgrade its lock, and if another
        # writer got there first that upgrade fails at once with "database is
        # locked" instead of waiting. Take the write lock up front (BEGIN
        # IMMEDIATE) so contention waits on busy_timeout, and use WAL so
        # readers never block on a writer.
        kwargs["connect_args"] = {"timeout": 30}
    engine = create_async_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):
        _configure_sqlite(engine)
    return engine


def _configure_sqlite(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:  # noqa: ANN001
        # Disable the driver's own BEGIN handling so the "begin" hook below
        # controls the transaction start (SQLAlchemy's documented recipe).
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn) -> None:  # noqa: ANN001
        conn.exec_driver_sql("BEGIN IMMEDIATE")


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
