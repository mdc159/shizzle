"""Unit: repository CRUD, pagination, events, tracks, idempotency-key uniqueness."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from shizzle_server.db.models import Job, JobStage, SourceType, utcnow
from shizzle_server.db.repository import (
    InvalidTransition,
    TrackGenerationConflict,
    pending_runpod_dispatch,
    track_id_for_job,
)
from shizzle_server.errors import ErrorCode


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


async def test_create_job_persists_artist_and_defaults_empty(job_repo):
    job = await job_repo.create_job(
        source_type=SourceType.upload,
        source_ref="source.mp4",
        title="Runnin' With The Devil",
        artist="Van Halen",
    )
    assert (await job_repo.get_job(job.id)).artist == "Van Halen"

    bare = await job_repo.create_job(source_type=SourceType.url, source_ref="https://x")
    fetched = await job_repo.get_job(bare.id)
    assert fetched.artist == ""  # never NULL: consumers read it unconditionally


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
    # Claim the lease, then drive the job to publishing so publish_track is legal.
    claimed = await job_repo.claim_next(worker_id="owner", lease_seconds=120)
    assert claimed is not None and claimed.id == upload_job.id
    for a, b in [
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.splitting),
        (JobStage.splitting, JobStage.verifying),
        (JobStage.verifying, JobStage.publishing),
    ]:
        await job_repo.advance(upload_job.id, from_stage=a, to_stage=b, worker_id="owner")
    track = await job_repo.publish_track(
        upload_job.id,
        worker_id="owner",
        title="Test Track",
        duration_seconds=12.5,
        s3_prefix=f"tracks/{track_id_for_job(upload_job.id)}/1",
        manifest_key=f"tracks/{track_id_for_job(upload_job.id)}/1/manifest.json",
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
    await job_repo.claim_next(worker_id="worker-a", lease_seconds=60)
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="worker-a",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="worker-a",
    )
    await job_repo.record_dispatch(
        upload_job.id, worker_id="worker-a", runpod_job_id="runpod-1"
    )
    dispatched = await job_repo.get_job(upload_job.id)
    assert dispatched is not None
    assert dispatched.runpod_job_id == "runpod-1"
    assert dispatched.worker_phase == "dispatched"
    first_heartbeat = dispatched.worker_heartbeat_at
    assert first_heartbeat is not None

    assert await job_repo.record_worker_progress(
        upload_job.id, phase="dispatched", worker_id="worker-a"
    ) is False
    assert await job_repo.record_worker_progress(
        upload_job.id, phase="separate", worker_id="worker-a"
    ) is True
    changed = await job_repo.get_job(upload_job.id)
    assert changed is not None
    assert changed.worker_phase == "separate"
    assert changed.worker_heartbeat_at is not None
    assert changed.worker_heartbeat_at >= first_heartbeat

    changed_heartbeat = changed.worker_heartbeat_at
    await asyncio.sleep(0.001)
    assert await job_repo.record_worker_progress(
        upload_job.id, phase="separate", worker_id="worker-a"
    ) is False
    refreshed = await job_repo.get_job(upload_job.id)
    assert refreshed is not None and refreshed.worker_heartbeat_at > changed_heartbeat
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

    await job_repo.claim_next(worker_id="worker-a", lease_seconds=60)
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="worker-a",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="worker-a",
    )
    with pytest.raises(InvalidTransition):
        await job_repo.record_dispatch(
            upload_job.id, worker_id="worker-b", runpod_job_id="runpod-1"
        )


async def test_record_dispatch_rejects_expired_or_missing_lease(job_repo, upload_job):
    await job_repo.claim_next(worker_id="worker-a", lease_seconds=60)
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="worker-a",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="worker-a",
    )

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
    await job_repo.claim_next(worker_id="worker-a", lease_seconds=60)
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="worker-a",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="worker-a",
    )
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
    await job_repo.claim_next(worker_id="worker-after-upgrade", lease_seconds=60)
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="worker-after-upgrade",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="worker-after-upgrade",
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

    await job_repo.record_dispatch(
        upload_job.id,
        worker_id="worker-after-upgrade",
        runpod_job_id="legacy-runpod-id",
    )
    assert pending_runpod_dispatch(
        await job_repo.list_events(upload_job.id)
    ) is None


async def test_stale_worker_cannot_fail_retry_or_advance_after_reclaim(job_repo, upload_job):
    """B2 fence on stage-outcome writes (issue #19 probe): after B reclaims
    A's expired lease, every A-attempted write is rejected and B's ownership
    plus the job state survive untouched."""
    t0 = utcnow()
    claimed_a = await job_repo.claim_next(worker_id="A", lease_seconds=5.0, now=t0)
    assert claimed_a is not None and claimed_a.id == upload_job.id
    reclaimed = await job_repo.claim_next(
        worker_id="B", lease_seconds=300.0, now=t0 + timedelta(seconds=60)
    )
    assert reclaimed is not None and reclaimed.lease_owner == "B"

    with pytest.raises(InvalidTransition):
        await job_repo.fail_job(
            upload_job.id,
            error_code=ErrorCode.INTERNAL,
            error_detail="stale worker failure",
            worker_id="A",
        )
    survived = await job_repo.get_job(upload_job.id)
    assert survived.status is JobStage.pending
    assert survived.lease_owner == "B"
    assert survived.attempt == 0

    with pytest.raises(InvalidTransition):
        await job_repo.schedule_retry(
            upload_job.id,
            worker_id="A",
            error_code=ErrorCode.DEMUCS_FAILED,
            error_detail="stale worker retry",
            retry_in_seconds=30,
        )
    survived = await job_repo.get_job(upload_job.id)
    assert survived.status is JobStage.pending
    assert survived.lease_owner == "B"
    assert survived.attempt == 0
    assert survived.next_retry_at is None

    with pytest.raises(InvalidTransition):
        await job_repo.advance(
            upload_job.id,
            from_stage=JobStage.pending,
            to_stage=JobStage.downloading,
            worker_id="A",
        )
    survived = await job_repo.get_job(upload_job.id)
    assert survived.status is JobStage.pending
    assert survived.lease_owner == "B"

    # Walk B to publishing so the ownership fence — not the stage guard —
    # is what rejects A's publish.
    for a, b in [
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.splitting),
        (JobStage.splitting, JobStage.verifying),
        (JobStage.verifying, JobStage.publishing),
    ]:
        await job_repo.advance(upload_job.id, from_stage=a, to_stage=b, worker_id="B")
    tid = track_id_for_job(upload_job.id)
    with pytest.raises(InvalidTransition):
        await job_repo.publish_track(
            upload_job.id,
            worker_id="A",
            title="Stale Publish",
            duration_seconds=1.0,
            s3_prefix=f"tracks/{tid}/1",
            manifest_key=f"tracks/{tid}/1/manifest.json",
        )
    survived = await job_repo.get_job(upload_job.id)
    assert survived.status is JobStage.publishing
    assert survived.lease_owner == "B"
    assert survived.track_id is None

    # The C7 refusal path writes an event, so it is fenced the same way: A's
    # invalid-location publish raises instead of recording publish_refused.
    with pytest.raises(InvalidTransition):
        await job_repo.publish_track(
            upload_job.id,
            worker_id="A",
            title="Stale Phantom",
            duration_seconds=1.0,
            s3_prefix=f"local/{upload_job.id.hex}",
            manifest_key=f"local/{upload_job.id.hex}/stems.json",
        )
    assert not any(
        event.event == "publish_refused"
        for event in await job_repo.list_events(upload_job.id)
    )
    survived = await job_repo.get_job(upload_job.id)
    assert survived.status is JobStage.publishing
    assert survived.lease_owner == "B"

    # Lower-severity sibling: A's progress poll is a silent no-op.
    assert (
        await job_repo.record_worker_progress(
            upload_job.id, phase="separating", worker_id="B"
        )
        is True
    )
    b_view = await job_repo.get_job(upload_job.id)
    assert b_view.worker_phase == "separating"
    assert (
        await job_repo.record_worker_progress(
            upload_job.id, phase="downloading", worker_id="A"
        )
        is False
    )
    after = await job_repo.get_job(upload_job.id)
    assert after.worker_phase == "separating"
    assert after.worker_heartbeat_at == b_view.worker_heartbeat_at


async def test_keyed_confirmation_does_not_clear_legacy_pending_dispatch(
    job_repo, upload_job
):
    await job_repo.append_event(
        upload_job.id,
        "dispatch_unconfirmed",
        {"attempt": 0, "error": "response lost before upgrade"},
    )
    await job_repo.append_event(
        upload_job.id,
        "runpod_dispatched",
        {"idempotency_key": "another-attempt", "runpod_job_id": "runpod-other"},
    )

    events = await job_repo.list_events(upload_job.id)
    assert pending_runpod_dispatch(events) is not None

    await job_repo.append_event(
        upload_job.id,
        "runpod_dispatched",
        {"runpod_job_id": "legacy-runpod-id"},
    )
    assert pending_runpod_dispatch(await job_repo.list_events(upload_job.id)) is None
