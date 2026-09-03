"""Invariant C7: a track row must point at real published objects.

Guards against the 2026-08-19 phantom track: job 9fe2c9a5 ran the stub
test pipeline in production (SHIZZLE_PIPELINE=test was the compose default),
flew pending -> publishing in under a second with verify detail
{test_pipeline: ...}, and published s3_prefix local/9fe2c9a5... with no
object in S3. The row listed in /api/library and could not play.

Three independent layers, each tested here:
1. build_pipeline refuses the test pipeline without the explicit opt-in flag.
2. publish_track refuses any location outside tracks/ + /manifest.json,
   recording a publish_refused job event instead of publishing.
3. list_tracks never returns rows outside tracks/.
Plus the full incident replay through the in-process orchestrator.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from shizzle_server.db.models import JobStage, Track
from shizzle_server.db.repository import (
    PublishRefusedError,
    track_id_for_job,
    track_location_problem,
)
from shizzle_server.orchestrator.loop import Orchestrator
from shizzle_server.orchestrator.pipelines import LocalPipeline, TestPipeline, build_pipeline
from shizzle_server.settings import Settings

# --- layer 1: the stub pipeline requires explicit opt-in ---------------------


def test_build_pipeline_refuses_test_pipeline_without_opt_in(settings):
    assert settings.shizzle_allow_test_pipeline is True  # suite fixture opts in
    settings.shizzle_allow_test_pipeline = False
    with pytest.raises(RuntimeError, match="SHIZZLE_ALLOW_TEST_PIPELINE"):
        build_pipeline(settings)


def test_build_pipeline_allows_test_pipeline_with_opt_in(settings):
    assert isinstance(build_pipeline(settings), TestPipeline)


def test_build_pipeline_local_profile_unaffected(settings):
    settings.shizzle_pipeline = "local"
    settings.shizzle_allow_test_pipeline = False
    assert isinstance(build_pipeline(settings), LocalPipeline)


def test_allow_test_pipeline_defaults_off():
    assert Settings.model_fields["shizzle_allow_test_pipeline"].default is False


# --- layer 2: publish_track refuses phantom locations -------------------------


async def _publishing_job(job_repo, upload_job):
    for a, b in [
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.splitting),
        (JobStage.splitting, JobStage.verifying),
        (JobStage.verifying, JobStage.publishing),
    ]:
        await job_repo.advance(upload_job.id, from_stage=a, to_stage=b)
    return upload_job


async def test_publish_track_refuses_local_prefix_and_records_event(
    job_repo, track_repo, upload_job
):
    """The exact 2026-08-19 phantom shape: local/ prefix, stems.json key."""
    await _publishing_job(job_repo, upload_job)

    with pytest.raises(PublishRefusedError, match="not under tracks/"):
        await job_repo.publish_track(
            upload_job.id,
            title="Phantom",
            duration_seconds=1.0,
            s3_prefix=f"local/{upload_job.id.hex}",
            manifest_key=f"local/{upload_job.id.hex}/stems.json",
        )

    # No track row, and the job is still in publishing (the stage handler
    # turns the refusal into a non-retryable PUBLISH_FAILED).
    assert await track_repo.get(track_id_for_job(upload_job.id)) is None
    assert await track_repo.list_tracks() == []
    job = await job_repo.get_job(upload_job.id)
    assert job.status == JobStage.publishing

    events = await job_repo.list_events(upload_job.id)
    refused = [e for e in events if e.event == "publish_refused"]
    assert len(refused) == 1
    assert refused[0].detail["s3_prefix"] == f"local/{upload_job.id.hex}"
    assert "not under tracks/" in refused[0].detail["reason"]
    assert "track_published" not in [e.event for e in events]


async def test_publish_track_refuses_non_manifest_key(job_repo, track_repo, upload_job):
    """A tracks/ prefix with a stems.json key is still the phantom shape."""
    await _publishing_job(job_repo, upload_job)
    with pytest.raises(PublishRefusedError, match="manifest.json"):
        await job_repo.publish_track(
            upload_job.id,
            title="Phantom",
            duration_seconds=1.0,
            s3_prefix=f"tracks/{track_id_for_job(upload_job.id)}/1",
            manifest_key=f"tracks/{track_id_for_job(upload_job.id)}/1/stems.json",
        )
    assert await track_repo.list_tracks() == []


async def test_publish_track_accepts_real_published_location(job_repo, track_repo, upload_job):
    await _publishing_job(job_repo, upload_job)
    tid = track_id_for_job(upload_job.id)
    track = await job_repo.publish_track(
        upload_job.id,
        title="Real",
        duration_seconds=12.5,
        s3_prefix=f"tracks/{tid}/1",
        manifest_key=f"tracks/{tid}/1/manifest.json",
    )
    assert track.id == tid
    assert [t.id for t in await track_repo.list_tracks()] == [tid]


async def test_publish_track_refuses_bare_tracks_prefix(job_repo, track_repo, upload_job):
    """`tracks/` alone names no published-object namespace — nothing to play."""
    await _publishing_job(job_repo, upload_job)
    with pytest.raises(PublishRefusedError, match="not under tracks/"):
        await job_repo.publish_track(
            upload_job.id,
            title="Phantom",
            duration_seconds=1.0,
            s3_prefix="tracks/",
            manifest_key="tracks/manifest.json",
        )
    assert await track_repo.list_tracks() == []


async def test_publish_track_refuses_manifest_outside_prefix(
    job_repo, track_repo, upload_job
):
    """A manifest key under a DIFFERENT prefix must not pass: the manifest
    endpoint would read unrelated S3 metadata for this track."""
    await _publishing_job(job_repo, upload_job)
    tid = track_id_for_job(upload_job.id)
    with pytest.raises(PublishRefusedError, match="manifest.json"):
        await job_repo.publish_track(
            upload_job.id,
            title="Phantom",
            duration_seconds=1.0,
            s3_prefix=f"tracks/{tid}/1",
            manifest_key="tracks/other/1/manifest.json",
        )
    assert await track_repo.list_tracks() == []


async def test_publish_track_refusal_is_recorded_once(job_repo, track_repo, upload_job):
    """Crash-rerun idempotency (B11): the refusal is deterministic, so a
    re-run after a crash appends no duplicate publish_refused event."""
    await _publishing_job(job_repo, upload_job)
    kwargs = dict(
        title="Phantom",
        duration_seconds=1.0,
        s3_prefix=f"local/{upload_job.id.hex}",
        manifest_key=f"local/{upload_job.id.hex}/stems.json",
    )
    with pytest.raises(PublishRefusedError):
        await job_repo.publish_track(upload_job.id, **kwargs)
    with pytest.raises(PublishRefusedError):
        await job_repo.publish_track(upload_job.id, **kwargs)

    refused = [
        e for e in await job_repo.list_events(upload_job.id) if e.event == "publish_refused"
    ]
    assert len(refused) == 1


def test_track_location_problem_messages():
    assert track_location_problem("tracks/x/1", "tracks/x/1/manifest.json") is None
    # A trailing slash on the prefix still matches its manifest.
    assert track_location_problem("tracks/x/1/", "tracks/x/1/manifest.json") is None
    assert "tracks/" in track_location_problem("local/abc", "local/abc/stems.json")
    assert "manifest.json" in track_location_problem("tracks/x/1", "tracks/x/1/stems.json")
    assert track_location_problem("tracks/", "tracks/manifest.json") is not None
    assert "manifest.json" in track_location_problem(
        "tracks/x/1", "tracks/other/1/manifest.json"
    )


# --- layer 3: the library never lists rows outside tracks/ --------------------


async def test_list_tracks_never_lists_non_tracks_prefix(track_repo, session_factory):
    good_id = uuid.uuid4()
    phantom_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            Track(
                id=good_id,
                title="Real",
                s3_prefix=f"tracks/{good_id}/1",
                manifest_key=f"tracks/{good_id}/1/manifest.json",
            )
        )
        # The 2026-08-19 row, inserted directly (publish_track now refuses it).
        session.add(
            Track(
                id=phantom_id,
                title="Phantom",
                duration_seconds=1.0,
                s3_prefix="local/9fe2c9a570f4449abf55bcd96f7159da",
                manifest_key="local/9fe2c9a570f4449abf55bcd96f7159da/stems.json",
            )
        )

    assert [t.id for t in await track_repo.list_tracks()] == [good_id]
    assert [t.id for t in await track_repo.list_tracks(include_deleted=True)] == [good_id]
    # Direct lookup by id still works (media serving decides playability).
    assert await track_repo.get(phantom_id) is not None


# --- the incident replay ------------------------------------------------------


async def _wait_for_status(repo, job_id, status: JobStage, timeout: float = 10.0):
    async def _poll():
        while True:
            job = await repo.get_job(job_id)
            if job is not None and job.status == status:
                return job
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def test_2026_08_19_phantom_replay_fails_closed(settings, job_repo, track_repo, upload_job):
    """Replay the incident: stub pipeline enabled (as production compose had
    it), upload job sails through every stage in well under a second, and the
    local/ placeholder publish is attempted. Now: refused, no track row, job
    failed with an explicit error_code, publish_refused on the record."""
    orch = Orchestrator(settings, worker_id="phantom-replay")
    task = asyncio.create_task(orch.run_forever())
    try:
        failed = await _wait_for_status(job_repo, upload_job.id, JobStage.failed, timeout=15)
    finally:
        orch.request_stop()
        await asyncio.wait_for(task, timeout=10)

    # The job really ran the stub pipeline: same evidence as production.
    events = await job_repo.list_events(upload_job.id)
    verifying = [
        e
        for e in events
        if e.event == "stage_completed" and e.detail.get("from") == "verifying"
    ]
    assert verifying and verifying[0].detail["verify"] == {"test_pipeline": True}

    # ...but this time nothing published.
    assert failed.error_code == "PUBLISH_FAILED"
    assert "not under tracks/" in failed.error_detail
    assert failed.attempt == 0  # non-retryable: no backoff loop, failed at once
    assert failed.track_id is None
    assert await track_repo.get(track_id_for_job(upload_job.id)) is None
    assert await track_repo.list_tracks() == []

    names = [e.event for e in events]
    assert "publish_refused" in names
    assert "failed" in names
    assert "track_published" not in names
