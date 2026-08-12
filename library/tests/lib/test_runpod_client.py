"""RunPod HTTP client and progress parser tests."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from shizzle_server.errors import ErrorCode, StageError
from shizzle_server.orchestrator.runpod_client import HttpRunPodClient, parse_worker_progress


def client(handler) -> HttpRunPodClient:
    return HttpRunPodClient(
        api_key="secret",
        endpoint_id="endpoint-1",
        api_base="https://runpod.test/v2",
        transport=httpx.MockTransport(handler),
    )


async def test_dispatch_captures_job_id_and_sends_expected_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://runpod.test/v2/endpoint-1/run"
        assert request.headers["Authorization"] == "Bearer secret"
        assert json.loads(request.content) == {"input": {"track_id": "track-1"}}
        return httpx.Response(200, json={"id": "runpod-job-1"})

    runpod_job_id = await client(handler).dispatch(
        job_id=uuid.uuid4(), idempotency_key="trace-1", payload={"track_id": "track-1"}
    )
    assert runpod_job_id == "runpod-job-1"


@pytest.mark.parametrize("status,retryable", [(401, False), (500, True)])
async def test_http_errors_are_mapped(status: int, retryable: bool) -> None:
    runpod = client(lambda _request: httpx.Response(status))
    with pytest.raises(StageError) as exc:
        await runpod.poll("job-1")
    assert exc.value.code == ErrorCode.RUNPOD_DISPATCH_FAILED
    assert exc.value.retryable is retryable


async def test_timeout_is_mapped() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(StageError) as exc:
        await client(timeout).poll("job-1")
    assert exc.value.code == ErrorCode.RUNPOD_TIMEOUT
    assert exc.value.retryable is True


async def test_breaker_opens_after_five_failures() -> None:
    calls = 0

    def unavailable(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    runpod = client(unavailable)
    for _ in range(5):
        with pytest.raises(StageError):
            await runpod.poll("job-1")
    with pytest.raises(StageError) as exc:
        await runpod.poll("job-1")
    assert calls == 5
    assert exc.value.code == ErrorCode.RUNPOD_DISPATCH_FAILED
    assert "circuit breaker" in exc.value.detail


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": "IN_QUEUE"}, ("IN_QUEUE", None)),
        ({"status": "IN_PROGRESS", "output": "acquire"}, ("IN_PROGRESS", "acquire")),
        (
            {"status": "IN_PROGRESS", "output": {"phase": "separate"}},
            ("IN_PROGRESS", "separate"),
        ),
        (
            {"status": "IN_PROGRESS", "output": ["acquire", {"progress": "upload"}]},
            ("IN_PROGRESS", "upload"),
        ),
        ({"status": "SOMETHING_NEW", "output": 42}, ("IN_PROGRESS", None)),
    ],
)
def test_parse_worker_progress_is_tolerant(
    payload: dict[str, object], expected: tuple[str, str | None]
) -> None:
    assert parse_worker_progress(payload) == expected
