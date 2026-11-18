"""DATABASE_URL must reach Alembic unchanged, including URL escapes (F2)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql+asyncpg://shizzle:synthetic@localhost/shizzle",
        "postgresql+asyncpg://shizzle:synthetic%40%3A%2F%23%25@localhost/shizzle",
    ],
)
def test_offline_migrations_preserve_database_url(monkeypatch, dsn):
    """Render real migrations without connecting to a database."""
    library = Path(__file__).resolve().parents[1]
    output = io.StringIO()
    config = Config(str(library / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(library / "alembic"))
    monkeypatch.setenv("DATABASE_URL", dsn)

    command.upgrade(config, "head", sql=True)

    assert config.get_main_option("sqlalchemy.url") == dsn
    assert "CREATE TABLE nodes" in output.getvalue()
