from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from shizzle_server.api.models import PlaybackTelemetryRequest
from shizzle_server.api.routes import _TelemetryRateLimiter


async def _track(track_repo):
    from shizzle_server.db.repository import track_id_for_import

    track_id = track_id_for_import("telemetry-track")
    await track_repo.upsert_imported(
        track_id,
        title="Telemetry Track",
        duration_seconds=10,
        s3_prefix=f"tracks/{track_id}/2",
        manifest_key=f"tracks/{track_id}/2/manifest.json",
        generation=2,
    )
    return track_id


async def test_telemetry_is_append_only_idempotent_and_updates_summary(
    track_repo, playback_telemetry_repo
):
    track_id = await _track(track_repo)
    session_id = uuid.uuid4()
    common = {
        "session_id": session_id,
        "track_id": track_id,
        "generation": 2,
        "client_at_ms": 1_785_913_678_439,
        "app_build": "build-1",
        "browser": "test-browser",
        "detail": {"videoTime": 1.25},
    }
    assert await playback_telemetry_repo.ingest(sequence=1, event="playing", **common)
    assert not await playback_telemetry_repo.ingest(sequence=1, event="playing", **common)
    assert await playback_telemetry_repo.ingest(sequence=2, event="heartbeat", **common)
    assert await playback_telemetry_repo.ingest(sequence=3, event="ended", **common)

    events = await playback_telemetry_repo.list_events(session_id)
    assert [event.sequence for event in events] == [1, 2, 3]
    assert events[0].client_at_ms == 1_785_913_678_439
    session = await playback_telemetry_repo.get_session(session_id)
    assert session is not None
    assert session.status == "ended"
    assert session.last_heartbeat_at is not None
    assert session.ended_at is not None


async def test_recent_incidents_identify_track_and_generation(track_repo, playback_telemetry_repo):
    track_id = await _track(track_repo)
    session_id = uuid.uuid4()
    common = {
        "session_id": session_id,
        "track_id": track_id,
        "generation": 2,
        "client_at_ms": 100,
        "app_build": "build-1",
        "browser": "test-browser",
        "detail": {"health": {"status": "recovering"}},
    }
    await playback_telemetry_repo.ingest(sequence=1, event="playing", **common)
    await playback_telemetry_repo.ingest(sequence=2, event="stem-clock-stalled", **common)
    rows = await playback_telemetry_repo.recent_incidents(
        since=datetime.now(UTC) - timedelta(minutes=1)
    )
    assert len(rows) == 1
    event, title = rows[0]
    assert event.event == "stem-clock-stalled"
    assert event.generation == 2
    assert title == "Telemetry Track"


async def test_telemetry_session_cannot_switch_track_generation(
    track_repo, playback_telemetry_repo
):
    track_id = await _track(track_repo)
    session_id = uuid.uuid4()
    common = {
        "session_id": session_id,
        "track_id": track_id,
        "generation": 2,
        "client_at_ms": 100,
        "app_build": "build-1",
        "browser": "test-browser",
        "detail": None,
    }
    await playback_telemetry_repo.ingest(sequence=1, event="playing", **common)
    with pytest.raises(ValueError, match="cannot change"):
        await playback_telemetry_repo.ingest(
            sequence=2, event="playing", **{**common, "generation": 3}
        )
    with pytest.raises(ValueError, match="behind"):
        await playback_telemetry_repo.ingest(sequence=0, event="playing", **common)


def test_telemetry_payload_rejects_credentials_and_unbounded_detail():
    base = {
        "sessionId": uuid.uuid4(),
        "trackId": uuid.uuid4(),
        "generation": 1,
        "sequence": 1,
        "event": "playing",
        "clientAtMs": 1,
    }
    with pytest.raises(ValidationError, match="forbidden key"):
        PlaybackTelemetryRequest(**base, detail={"authorization": "Bearer secret"})
    with pytest.raises(ValidationError, match="2048"):
        PlaybackTelemetryRequest(**base, detail={"message": "x" * 2049})
    accepted = PlaybackTelemetryRequest(**base, detail={"video": {"currentTime": 1.2}})
    assert accepted.event == "playing"


def test_telemetry_rate_limiter_bounds_and_reopens_window(monkeypatch):
    clock = iter([0.0, 0.1, 0.2, 10.1])
    monkeypatch.setattr("shizzle_server.api.routes.time.monotonic", lambda: next(clock))
    limiter = _TelemetryRateLimiter(limit=2, window_seconds=10)
    session_id = uuid.uuid4()

    assert limiter.allow(session_id)
    assert limiter.allow(session_id)
    assert not limiter.allow(session_id)
    assert limiter.allow(session_id)
