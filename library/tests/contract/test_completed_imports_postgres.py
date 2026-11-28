"""Real migrated Postgres: duplicate contributions and claims serialize."""

import asyncio
import hashlib

import pytest
from sqlalchemy import select

from shizzle_server.db import create_session_factory
from shizzle_server.db.models import CompletedImport, CompletedImportEvent
from shizzle_server.publish.completed import CANONICAL_STEM_IDS, FILES, ImportRepository

pytestmark = pytest.mark.postgres


def candidate():
    return {
        "version": 3,
        "delivery_profile": "shizzle-browser-v1",
        "title": "PG fixture",
        "artist": "Fixture",
        "duration": 10,
        "video": "video.mp4",
        "timeline": {"start_ms": 0, "duration_ms": 10000, "sample_rate_hz": 44100},
        "stems": [
            {"id": r, "name": r, "file": f"stems/{r}.m4a", "default_gain_db": -6}
            for r in CANONICAL_STEM_IDS
        ],
        "integrity": {
            "objects": [
                {"file": f, "bytes": len(f), "sha256": hashlib.sha256(f.encode()).hexdigest()}
                for f in FILES
            ]
        },
    }


async def test_duplicate_create_finalize_and_claim(pg_engine):
    sf = create_session_factory(pg_engine)
    repo = ImportRepository(sf)
    items = await asyncio.gather(*(repo.create(candidate()) for _ in range(8)))
    assert len({item.id for item in items}) == 1
    identifier = items[0].id
    await asyncio.gather(*(repo.finalize(identifier) for _ in range(8)))
    claims = await asyncio.gather(*(repo.claim(f"worker-{i}", 30) for i in range(8)))
    assert len([claim for claim in claims if claim]) == 1
    async with sf() as session:
        assert len((await session.scalars(select(CompletedImport))).all()) == 1
        assert (
            await session.scalars(
                select(CompletedImportEvent.event).order_by(CompletedImportEvent.id)
            )
        ).all() == ["uploading", "validating"]


async def test_expired_lease_fences_stale_worker(pg_engine):
    repo = ImportRepository(create_session_factory(pg_engine))
    item = await repo.create(candidate())
    await repo.finalize(item.id)
    await repo.claim("old", -1)
    assert (await repo.claim("new", 30)).id == item.id
    await repo.failure(item.id, "old", "STALE", False)
    assert (await repo.get(item.id)).lease_owner == "new"
    await repo.failure(item.id, "new", "RETRY", True)
    row = await repo.get(item.id)
    assert row.next_retry_at is not None and row.status == "validating"
