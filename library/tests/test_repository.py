"""Unit: repository CRUD, pagination, events, tracks, idempotency-key uniqueness."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from shizzle_server.db.models import Job, JobStage, SourceType, utcnow
from shizzle_server.db.repository import (
    InvalidTransition,
    TrackGenerationConflict,
    track_id_for_job,
)


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


async def test_generation_activation_is_compare_and_swap_with_append_only_event(track_repo):
    from shizzle_server.db.repository import track_id_for_import

    track_id = track_id_for_import("activation-test")
    await track_repo.upsert_imported(
        track_id,
        title="Activation Test",
        duration_seconds=10,
        s3_prefix=f"tracks/{track_id}/1",
        manifest_key=f"tracks/{track_id}/1/manifest.json",
        generation=1,
        integrity={"source": "legacy"},
    )

    activated = await track_repo.activate_generation(
        track_id,
        expected_generation=1,
        generation=2,
        s3_prefix=f"tracks/{track_id}/2/",
        manifest_key=f"tracks/{track_id}/2/manifest.json",
        integrity={"source": "audited-migration", "passed": True},
        detail={"report_id": "report-123"},
    )
    assert activated.generation == 2
    assert activated.s3_prefix == f"tracks/{track_id}/2"
    events = await track_repo.list_generation_events(track_id)
    assert len(events) == 1
    assert events[0].event == "activated"
    assert events[0].from_generation == 1
    assert events[0].to_generation == 2
    assert events[0].detail["report_id"] == "report-123"
    assert events[0].detail["prior"]["manifest_key"].endswith("/1/manifest.json")

    with pytest.raises(TrackGenerationConflict) as exc:
        await track_repo.activate_generation(
            track_id,
            expected_generation=1,
            generation=3,
            s3_prefix=f"tracks/{track_id}/3",
            manifest_key=f"tracks/{track_id}/3/manifest.json",
            integrity={"source": "stale"},
        )
    assert exc.value.actual == 2
    assert len(await track_repo.list_generation_events(track_id)) == 1


async def test_generation_rollback_uses_same_atomic_ledger(track_repo):
    from shizzle_server.db.repository import track_id_for_import

    track_id = track_id_for_import("rollback-test")
    await track_repo.upsert_imported(
        track_id,
        title="Rollback Test",
        duration_seconds=10,
        s3_prefix=f"tracks/{track_id}/2",
        manifest_key=f"tracks/{track_id}/2/manifest.json",
        generation=2,
        integrity={"source": "new"},
    )
    rolled_back = await track_repo.activate_generation(
        track_id,
        expected_generation=2,
        generation=1,
        s3_prefix=f"tracks/{track_id}/1",
        manifest_key=f"tracks/{track_id}/1/manifest.json",
        integrity={"source": "legacy"},
        event="rollback",
        detail={"reason": "drill"},
    )
    assert rolled_back.generation == 1
    event = (await track_repo.list_generation_events(track_id))[0]
    assert event.event == "rollback"
    assert event.detail["reason"] == "drill"


async def test_park_frees_lease_without_consuming_attempt_or_adding_event(job_repo, upload_job):
    claimed = await job_repo.claim_next(worker_id="worker-a", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id

    await job_repo.park(upload_job.id, worker_id="worker-a", recheck_in_seconds=10)

    parked = await job_repo.get_job(upload_job.id)
    assert parked is not None
    assert parked.attempt == 0
    assert parked.next_retry_at is not None
    assert parked.lease_owner is None
    assert parked.lease_expires_at is None
    assert [event.event for event in await job_repo.list_events(upload_job.id)] == ["created"]


async def test_worker_progress_writes_only_on_phase_change(job_repo, upload_job):
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    await job_repo.claim_next(worker_id="worker-a", lease_seconds=60)
    await job_repo.record_dispatch(
        upload_job.id, worker_id="worker-a", runpod_job_id="runpod-1"
    )
    dispatched = await job_repo.get_job(upload_job.id)
    assert dispatched is not None
    assert dispatched.runpod_job_id == "runpod-1"
    assert dispatched.worker_phase == "dispatched"
    first_heartbeat = dispatched.worker_heartbeat_at
    assert first_heartbeat is not None

    assert await job_repo.record_worker_progress(upload_job.id, phase="dispatched") is False
    assert await job_repo.record_worker_progress(upload_job.id, phase="separate") is True
    changed = await job_repo.get_job(upload_job.id)
    assert changed is not None
    assert changed.worker_phase == "separate"
    assert changed.worker_heartbeat_at is not None
    assert changed.worker_heartbeat_at >= first_heartbeat

    changed_heartbeat = changed.worker_heartbeat_at
    assert await job_repo.record_worker_progress(upload_job.id, phase="separate") is False
    refreshed = await job_repo.get_job(upload_job.id)
    assert refreshed is not None and refreshed.worker_heartbeat_at >= changed_heartbeat
    events = await job_repo.list_events(upload_job.id)
    assert [event.event for event in events] == [
        "created",
        "stage_completed",
        "stage_completed",
        "runpod_dispatched",
        "worker_progress",
    ]
    assert events[-1].detail == {"phase": "separate"}


async def test_record_dispatch_requires_dispatched_stage_and_lease_owner(job_repo, upload_job):
    with pytest.raises(InvalidTransition):
        await job_repo.record_dispatch(
            upload_job.id, worker_id="worker-a", runpod_job_id="runpod-1"
        )

    await job_repo.advance(
        upload_job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    await job_repo.claim_next(worker_id="worker-a", lease_seconds=60)
    with pytest.raises(InvalidTransition):
        await job_repo.record_dispatch(
            upload_job.id, worker_id="worker-b", runpod_job_id="runpod-1"
        )


async def test_record_dispatch_rejects_expired_or_missing_lease(job_repo, upload_job):
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    await job_repo.claim_next(worker_id="worker-a", lease_seconds=60)

    for expiry in (utcnow() - timedelta(seconds=1), None):
        async with job_repo._sf() as session, session.begin():
            row = await session.get(Job, upload_job.id)
            row.lease_expires_at = expiry
        with pytest.raises(InvalidTransition):
            await job_repo.record_dispatch(
                upload_job.id, worker_id="worker-a", runpod_job_id="runpod-1"
            )


async def test_dispatch_reservation_blocks_duplicate_and_survives_lease_turnover(
    job_repo, upload_job
):
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    await job_repo.claim_next(worker_id="worker-a", lease_seconds=60)
    dispatch_key = f"{upload_job.id.hex}:0"

    assert await job_repo.reserve_dispatch(
        upload_job.id,
        worker_id="worker-a",
        idempotency_key=dispatch_key,
    )
    assert not await job_repo.reserve_dispatch(
        upload_job.id,
        worker_id="worker-a",
        idempotency_key=dispatch_key,
    )

    async with job_repo._sf() as session, session.begin():
        row = await session.get(Job, upload_job.id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
    reclaimed = await job_repo.claim_next(worker_id="worker-b", lease_seconds=60)
    assert reclaimed is not None and reclaimed.lease_owner == "worker-b"

    # The original caller may still persist the accepted RunPod id. The
    # reservation key, rather than lease ownership, prevents stale overwrite.
    assert await job_repo.confirm_dispatch(
        upload_job.id,
        idempotency_key=dispatch_key,
        runpod_job_id="runpod-accepted",
    )
    assert not await job_repo.confirm_dispatch(
        upload_job.id,
        idempotency_key=dispatch_key,
        runpod_job_id="runpod-accepted",
    )
    job = await job_repo.get_job(upload_job.id)
    assert job.runpod_job_id == "runpod-accepted"
    assert job.worker_phase == "dispatched"
    events = await job_repo.list_events(upload_job.id)
    assert [event.event for event in events].count("runpod_dispatch_started") == 1
    confirmations = [event for event in events if event.event == "runpod_dispatched"]
    assert len(confirmations) == 1
    assert confirmations[0].detail == {
        "runpod_job_id": "runpod-accepted",
        "idempotency_key": dispatch_key,
    }


async def test_legacy_unconfirmed_dispatch_blocks_redispatch_after_upgrade(
    job_repo, upload_job
):
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    await job_repo.append_event(
        upload_job.id,
        "dispatch_unconfirmed",
        {"attempt": 0, "error": "response lost before upgrade"},
    )
    await job_repo.append_event(upload_job.id, "dispatch_unconfirmed_grace")
    await job_repo.claim_next(worker_id="worker-after-upgrade", lease_seconds=60)

    assert not await job_repo.reserve_dispatch(
        upload_job.id,
        worker_id="worker-after-upgrade",
        idempotency_key=f"{upload_job.id.hex}:0",
    )
