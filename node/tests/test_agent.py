"""Agent loop and executors with a fake control plane (httpx MockTransport).

No network, no yt-dlp, no torch: the download and separation executors are
injected, and the HTTP layer is replayed through an in-memory fake that
records every call the agent makes.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest

from shizzle_node.agent import Agent
from shizzle_node.client import ApiError, Assignment, NodeClient
from shizzle_node.config import ConfigError, NodeConfig
from shizzle_node.download import DownloadError, SourceFile, build_receipt


class FakePlane:
    """Scripted node API: queue of assignments, records of what the agent sent."""

    def __init__(self) -> None:
        self.queue: list[dict] = []
        self.calls: list[tuple[str, str, dict | None]] = []
        self.ack_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        assert request.headers["authorization"] == "Bearer tok"
        if path.endswith("/heartbeat"):
            return httpx.Response(200, json={"ok": True, "serverTime": "2026-11-05T00:00:00Z"})
        if path.endswith("/assignments/next"):
            if self.queue:
                return httpx.Response(200, json=self.queue.pop(0))
            return httpx.Response(204)
        if path.endswith("/ack"):
            return httpx.Response(self.ack_status, json={"detail": "nope"} if self.ack_status >= 400 else {"ok": True})
        if path.endswith(("/progress", "/complete", "/fail")):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "unknown"})

    def sent(self, suffix: str) -> list[dict | None]:
        return [body for method, path, body in self.calls if path.endswith(suffix)]


@pytest.fixture
def config(tmp_path) -> NodeConfig:
    return NodeConfig.from_env(
        {
            "SHIZZLE_API_BASE": "https://shizzle.test",
            "SHIZZLE_NODE_ID": "mac-mini",
            "SHIZZLE_NODE_TOKEN": "tok",
            "SHIZZLE_NODE_DISPLAY_NAME": "Mac mini",
            "SHIZZLE_NODE_DEVICE": "mps",
            "SHIZZLE_NODE_WORKDIR": str(tmp_path / "work"),
            "SHIZZLE_STEMSPLIT_DIR": str(tmp_path / "stemsplit"),
        }
    )


@pytest.fixture
def plane() -> FakePlane:
    return FakePlane()


@pytest.fixture
def client(plane) -> NodeClient:
    return NodeClient(
        "https://shizzle.test", "mac-mini", "tok", transport=httpx.MockTransport(plane.handler)
    )


def _assignment(kind: str, payload: dict, *, key: str | None = None) -> dict:
    return {
        "assignmentId": str(uuid.uuid4()),
        "jobId": uuid.uuid4().hex,
        "kind": kind,
        "status": "assigned",
        "idempotencyKey": key,
        "payload": payload,
        "createdAt": "2026-11-05T00:00:00Z",
    }


def _source(tmp_path: Path) -> SourceFile:
    path = tmp_path / "source.mp4"
    path.write_bytes(b"mp4")
    return SourceFile(
        path=path, bytes=3, sha256="a" * 64, duration_seconds=12.5, title="Song (Official)",
        uploader="Band", video_id="dQw4w9WgXcQ", downloader="yt-dlp test",
    )


# --- config --------------------------------------------------------------------


def test_config_requires_the_essentials_and_validates_choices():
    with pytest.raises(ConfigError, match="SHIZZLE_API_BASE"):
        NodeConfig.from_env({})
    base = {"SHIZZLE_API_BASE": "https://x", "SHIZZLE_NODE_ID": "n", "SHIZZLE_NODE_TOKEN": "t"}
    assert NodeConfig.from_env(base).device == "auto"
    assert NodeConfig.from_env(base).capabilities == frozenset({"download", "separate"})
    with pytest.raises(ConfigError, match="DEVICE"):
        NodeConfig.from_env({**base, "SHIZZLE_NODE_DEVICE": "tpu"})
    with pytest.raises(ConfigError, match="capabilities"):
        NodeConfig.from_env({**base, "SHIZZLE_NODE_CAPABILITIES": "download,fly"})
    with pytest.raises(ConfigError, match="NODE_ID"):
        NodeConfig.from_env({**base, "SHIZZLE_NODE_ID": "bad id!"})
    only = NodeConfig.from_env({**base, "SHIZZLE_NODE_CAPABILITIES": "download"})
    assert only.capability_payload(resolved_device="none") == {
        "download": True, "separate": False, "device": "none", "agent": f"shizzle-node/{only.agent_version}",
    }


# --- client ----------------------------------------------------------------------


def test_client_next_returns_none_on_204_and_raises_on_errors(client, plane):
    assert client.next_assignment(wait_seconds=0) is None
    plane.queue.append(_assignment("download", {"url": "u"}))
    got = client.next_assignment(wait_seconds=0)
    assert isinstance(got, Assignment) and got.kind == "download" and got.payload == {"url": "u"}
    plane.ack_status = 409
    with pytest.raises(ApiError) as excinfo:
        client.ack(got.id)
    assert excinfo.value.status == 409 and "nope" in excinfo.value.detail


# --- download executor ----------------------------------------------------------


def test_download_assignment_runs_download_upload_and_reports(config, client, plane, tmp_path):
    payload = {
        "url": "https://youtu.be/dQw4w9WgXcQ", "source_ref": "youtube-dQw4w9WgXcQ",
        "track_id": "11111111-1111-4111-8111-111111111111", "bucket": "b",
        "source_key": "sources/11111111-1111-4111-8111-111111111111/source.mp4",
        "source_json_key": "sources/11111111-1111-4111-8111-111111111111/source.json",
        "max_duration_seconds": 1800, "max_bytes": 100, "title": None, "artist": "",
    }
    seen: dict = {}

    def fake_download(url, workdir, *, max_bytes, max_duration_seconds, cookies_file, progress):
        seen["download"] = (url, max_bytes, max_duration_seconds, cookies_file)
        progress("download: 50%")
        return _source(tmp_path)

    def fake_upload(s3, *, bucket, source, receipt, progress):
        seen["upload"] = (s3, bucket, receipt)
        progress("upload: source.json")
        return receipt

    agent = Agent(
        config, client, s3_factory=lambda: "S3", downloader=fake_download, uploader=fake_upload,
        separator=lambda *a, **k: {}, device_resolver=lambda d: d,
    )
    assignment = Assignment.from_json(_assignment("download", payload))
    agent.execute(assignment)

    assert seen["download"] == ("https://youtu.be/dQw4w9WgXcQ", 100, 1800.0, None)
    s3, bucket, receipt = seen["upload"]
    assert (s3, bucket) == ("S3", "b")
    assert receipt["interface"] == "shizzle-source-v1"
    assert receipt["object_key"] == payload["source_key"]
    assert receipt["track_id"] == payload["track_id"]
    assert receipt["sha256"] == "a" * 64 and receipt["bytes"] == 3
    assert receipt["title"] == "Song (Official)" and receipt["uploader"] == "Band"
    assert receipt["node_id"] == "mac-mini" and receipt["assignment_id"] == str(assignment.id)
    assert [c[1].rsplit("/", 1)[-1] for c in plane.calls] == [
        "ack", "heartbeat", "progress", "progress", "complete", "heartbeat",
    ]
    [done] = plane.sent("/complete")
    assert done["detail"]["sha256"] == "a" * 64 and done["detail"]["title"] == "Song (Official)"
    assert done["detail"]["adopted"] is False
    # Busy pointer rode along on the in-flight heartbeat and was cleared after.
    beats = plane.sent("/heartbeat")
    assert beats[0]["busyAssignmentId"] == str(assignment.id) and beats[1]["busyAssignmentId"] is None
    assert beats[0]["capabilities"]["device"] == "mps"


def test_download_failure_reports_the_structured_code(config, client, plane):
    def failing(*_a, **_k):
        raise DownloadError("YOUTUBE_SIGN_IN_REQUIRED", "Sign in to confirm you're not a bot")

    agent = Agent(
        config, client, s3_factory=lambda: None, downloader=failing,
        uploader=lambda *a, **k: None, separator=lambda *a, **k: {}, device_resolver=lambda d: d,
    )
    agent.execute(Assignment.from_json(_assignment("download", {
        "url": "u", "track_id": "t", "bucket": "b", "source_key": "k",
    })))
    [failed] = plane.sent("/fail")
    assert failed == {"code": "YOUTUBE_SIGN_IN_REQUIRED", "detail": "Sign in to confirm you're not a bot"}
    assert plane.sent("/complete") == []


def test_bad_payload_is_reported_not_crashed(config, client, plane):
    agent = Agent(
        config, client, s3_factory=lambda: None, downloader=lambda *a, **k: None,
        uploader=lambda *a, **k: None, separator=lambda *a, **k: {}, device_resolver=lambda d: d,
    )
    agent.execute(Assignment.from_json(_assignment("download", {"url": "u"})))
    [failed] = plane.sent("/fail")
    assert failed["code"] == "BAD_PAYLOAD" and "track_id" in failed["detail"]


# --- separation executor --------------------------------------------------------


def test_separate_assignment_calls_the_handler_with_executor_id_and_device(config, client, plane):
    payload = {
        "track_id": "t", "generation": 1, "bucket": "b", "input_key": "sources/t/source.mp4",
        "output_prefix": "tracks/t/1/separation", "idempotency_key": "job:0",
    }
    seen: dict = {}

    def fake_separate(p, *, executor_job_id, device, worker_image, stemsplit_dir, progress):
        seen.update(payload=p, executor=executor_job_id, device=device, image=worker_image,
                    stemsplit=stemsplit_dir)
        progress("separate: 100%")
        return {"status": "COMPLETED", "package_prefix": "tracks/t/1/separation/attempts/x",
                "sample_count": 10, "uploads": 7, "timings": {"separate": 18.2}}

    agent = Agent(
        config, client, s3_factory=lambda: None, downloader=lambda *a, **k: None,
        uploader=lambda *a, **k: None, separator=fake_separate, device_resolver=lambda d: d,
    )
    assignment = Assignment.from_json(_assignment("separate", payload, key="job:0"))
    agent.execute(assignment)
    assert seen["payload"] == payload
    assert seen["executor"] == f"node:{assignment.id}"
    assert seen["device"] == "mps"
    assert seen["image"] == f"node:mac-mini/shizzle-node-{config.agent_version}/mps"
    assert seen["stemsplit"] == config.stemsplit_dir
    [done] = plane.sent("/complete")
    assert done["detail"]["package_prefix"] == "tracks/t/1/separation/attempts/x"
    assert done["detail"]["device"] == "mps"


def test_attempt_claimed_is_reported_with_a_stable_code(config, client, plane):
    class AttemptClaimedError(RuntimeError):
        pass

    def claimed(*_a, **_k):
        raise AttemptClaimedError("attempt x is claimed by execution y")

    agent = Agent(
        config, client, s3_factory=lambda: None, downloader=lambda *a, **k: None,
        uploader=lambda *a, **k: None, separator=claimed, device_resolver=lambda d: d,
    )
    agent.execute(Assignment.from_json(_assignment("separate", {
        "track_id": "t", "bucket": "b", "input_key": "k", "output_prefix": "p",
        "idempotency_key": "j:0",
    })))
    [failed] = plane.sent("/fail")
    assert failed["code"] == "ATTEMPT_CLAIMED"


def test_unsupported_kind_and_lost_ack_do_not_run_work(config, client, plane, tmp_path):
    download_only = NodeConfig.from_env({
        "SHIZZLE_API_BASE": "https://shizzle.test", "SHIZZLE_NODE_ID": "mac-mini",
        "SHIZZLE_NODE_TOKEN": "tok", "SHIZZLE_NODE_CAPABILITIES": "download",
        "SHIZZLE_NODE_WORKDIR": str(tmp_path),
    })
    ran = []
    agent = Agent(
        download_only, client, s3_factory=lambda: None,
        downloader=lambda *a, **k: ran.append("download") or _source(tmp_path),
        uploader=lambda *a, **k: None, separator=lambda *a, **k: ran.append("separate") or {},
        device_resolver=lambda d: d,
    )
    assert agent.device == "none"
    agent.execute(Assignment.from_json(_assignment("separate", {})))
    assert plane.sent("/fail")[0]["code"] == "UNSUPPORTED_KIND" and ran == []
    # An ack the server refuses (expired/reassigned) is skipped silently.
    plane.ack_status = 409
    agent.execute(Assignment.from_json(_assignment("download", {
        "url": "u", "track_id": "t", "bucket": "b", "source_key": "k",
    })))
    assert ran == [] and len(plane.sent("/fail")) == 1


# --- receipt ---------------------------------------------------------------------


def test_build_receipt_matches_the_orchestrator_contract(tmp_path):
    receipt = build_receipt(
        _source(tmp_path),
        payload={"track_id": "t", "source_key": "sources/t/source.mp4", "source_ref": "youtube-x"},
        node_id="dell-9530",
    )
    assert set(receipt) >= {
        "interface", "track_id", "object_key", "bytes", "sha256", "duration_seconds",
        "source_ref", "video_id", "title", "uploader", "downloader", "node_id", "downloaded_at",
    }
    assert receipt["interface"] == "shizzle-source-v1"
    assert receipt["duration_seconds"] == 12.5
    json.dumps(receipt, allow_nan=False)  # serializable, no NaN


def test_adopted_receipt_is_what_gets_reported(config, client, plane, tmp_path):
    """When another node finished the same video first, the report describes
    the receipt in S3, not this run's download."""
    def fake_upload(s3, *, bucket, source, receipt, progress):
        return {"sha256": "b" * 64, "bytes": 99, "duration_seconds": 3.0, "title": "Theirs",
                "uploader": "Them", "video_id": "dQw4w9WgXcQ"}

    agent = Agent(
        config, client, s3_factory=lambda: None, downloader=lambda *a, **k: _source(tmp_path),
        uploader=fake_upload, separator=lambda *a, **k: {}, device_resolver=lambda d: d,
    )
    agent.execute(Assignment.from_json(_assignment("download", {
        "url": "https://youtu.be/dQw4w9WgXcQ", "track_id": "t", "bucket": "b",
        "source_key": "sources/t/source.mp4",
    })))
    [done] = plane.sent("/complete")
    assert done["detail"]["sha256"] == "b" * 64 and done["detail"]["adopted"] is True
