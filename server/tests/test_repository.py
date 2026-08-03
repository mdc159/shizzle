"""Unit: repository CRUD, pagination, events, tracks, idempotency-key uniqueness."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from shizzle_server.db.models import JobStage, SourceType
from shizzle_server.db.repository import track_id_for_job


async def test_create_and_get_job(job_repo):
    job = await job_repo.create_job(
        source_type=SourceType.upload, source_ref="source.mp4", title="My Song"
    )
    fetched = await job_repo.get_job(job.id)
    assert fetched is not None
    assert fetched.status == JobStage.pending
    assert fetched.title == "My Song"
    assert fetched.attempt == 0
    assert fetched.idempotency_key

    events = await job_repo.list_events(job.id)
    assert [e.event for e in events] == ["created"]


async def test_get_missing_job_returns_none(job_repo):
    assert await job_repo.get_job(uuid.uuid4()) is None


async def test_idempotency_key_unique(job_repo):
    await job_repo.create_job(
        source_type=SourceType.url, source_ref="https://x", idempotency_key="dup-key"
    )
    with pytest.raises(IntegrityError):
        await job_repo.create_job(
            source_type=SourceType.url, source_ref="https://y", idempotency_key="dup-key"
        )
    found = await job_repo.get_job_by_idempotency_key("dup-key")
    assert found is not None
    assert found.source_ref == "https://x"


async def test_list_jobs_newest_first_paginated(job_repo):
    created = []
    for i in range(5):
        created.append(
            await job_repo.create_job(
                source_type=SourceType.url, source_ref=f"https://x/{i}", title=f"t{i}"
            )
        )
    page1, total = await job_repo.list_jobs(limit=2, offset=0)
    page2, _ = await job_repo.list_jobs(limit=2, offset=2)
    assert total == 5
    assert len(page1) == 2 and len(page2) == 2
    all_ids = [j.id for j in page1 + page2]
    assert len(set(all_ids)) == 4
    # Newest first
    listed, _ = await job_repo.list_jobs(limit=10)
    times = [j.created_at for j in listed]
    assert times == sorted(times, reverse=True)


async def test_events_append_only_ordering(job_repo, upload_job):
    await job_repo.append_event(upload_job.id, "custom_one", {"n": 1})
    await job_repo.append_event(upload_job.id, "custom_two", {"n": 2})
    events = await job_repo.list_events(upload_job.id)
    assert [e.event for e in events] == ["created", "custom_one", "custom_two"]
    assert events[1].detail == {"n": 1}


async def test_track_soft_delete_and_library_filter(job_repo, track_repo, upload_job):
    # Drive the job to publishing so publish_track is legal.
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.downloading, to_stage=JobStage.splitting
    )
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.splitting, to_stage=JobStage.verifying
    )
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.verifying, to_stage=JobStage.publishing
    )
    track = await job_repo.publish_track(
        upload_job.id,
        title="Test Track",
        duration_seconds=12.5,
        s3_prefix=f"local/{upload_job.id.hex}",
        manifest_key=f"local/{upload_job.id.hex}/stems.json",
    )
    assert track.id == track_id_for_job(upload_job.id)

    listed = await track_repo.list_tracks()
    assert [t.id for t in listed] == [track.id]

    assert await track_repo.soft_delete(track.id) is True
    assert await track_repo.list_tracks() == []
    assert len(await track_repo.list_tracks(include_deleted=True)) == 1
    # Second delete is a no-op
    assert await track_repo.soft_delete(track.id) is False


async def test_heartbeat_upsert_and_latest(heartbeat_repo):
    assert await heartbeat_repo.latest() is None
    await heartbeat_repo.beat("worker-a")
    first = await heartbeat_repo.latest()
    assert first is not None
    await heartbeat_repo.beat("worker-a")  # update, not duplicate insert
    second = await heartbeat_repo.latest()
    assert second is not None and second >= first
