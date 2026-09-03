"""Pipeline implementations behind the splitting/verifying/publishing stages.

Two implementations:

- LocalPipeline — reuses processing.run_pipeline (ffmpeg + Demucs on this
  host/container). Keeps the Phase 1 golden-clip flow working, now DB-backed.
- TestPipeline — deterministic marker-file pipeline for fault-injection tests
  (kill/restart, retry schedules). Selected with SHIZZLE_PIPELINE=test plus
  the explicit SHIZZLE_ALLOW_TEST_PIPELINE=1 opt-in; it can never run in
  normal operation (invariant C7).

Both are idempotent per stage: work already completed (marker/manifest on
disk) is skipped on re-run, so a crashed orchestrator can safely re-execute
the stage it died in.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Protocol

from ..errors import ErrorCode, StageError
from ..settings import Settings


class Pipeline(Protocol):
    """What the stage handlers need from a media pipeline."""

    async def split(self, job_dir: Path, source_path: Path, title: str) -> dict[str, float]:
        """Produce stems + manifest under job_dir. Returns sub-stage timings.

        Must be idempotent: if the manifest already exists, return immediately.
        """
        ...

    async def verify(self, job_dir: Path) -> dict[str, Any]:
        """Check produced artifacts; raise StageError on structural problems."""
        ...

    async def describe(self, job_dir: Path) -> dict[str, Any]:
        """Read title/duration/etc. from the produced manifest for publishing."""
        ...


class LocalPipeline:
    """Adapter over processing.run_pipeline (Phase 1 GPU reference pipeline)."""

    MANIFEST = "stems.json"
    REQUIRED = ("video.mp4", "stems.json")

    async def split(self, job_dir: Path, source_path: Path, title: str) -> dict[str, float]:
        from . import processing

        if (job_dir / self.MANIFEST).exists():
            return {}  # already done before a crash — idempotent no-op

        timings: dict[str, float] = {}
        last: list = []  # [stage, started_monotonic]

        def on_status(status: str, _message: str) -> None:
            now = time.monotonic()
            if last:
                timings[last[0]] = round(timings.get(last[0], 0.0) + now - last[1], 2)
            last[:] = [status, now]

        try:
            await asyncio.to_thread(
                processing.run_pipeline, job_dir, source_path, on_status, title
            )
        except Exception as e:  # map subprocess failures to structured codes
            msg = str(e)
            if "demucs" in msg.lower():
                raise StageError(ErrorCode.DEMUCS_FAILED, msg[:500]) from e
            raise StageError(ErrorCode.FFMPEG_FAILED, msg[:500]) from e
        if last:
            timings[last[0]] = round(timings.get(last[0], 0.0) + time.monotonic() - last[1], 2)
        return timings

    async def verify(self, job_dir: Path) -> dict[str, Any]:
        missing = [name for name in self.REQUIRED if not (job_dir / name).exists()]
        manifest = await self.describe(job_dir)
        for stem in manifest.get("stems", []):
            if not (job_dir / stem["file"]).exists():
                missing.append(stem["file"])
        if missing:
            raise StageError(
                ErrorCode.CHECKSUM_MISMATCH,
                f"expected artifacts missing after splitting: {missing}",
                retryable=False,
            )
        return {"verified_files": len(manifest.get("stems", [])) + len(self.REQUIRED)}

    async def describe(self, job_dir: Path) -> dict[str, Any]:
        manifest_path = job_dir / self.MANIFEST
        if not manifest_path.exists():
            raise StageError(
                ErrorCode.CHECKSUM_MISMATCH, "stems.json missing", retryable=False
            )
        return json.loads(manifest_path.read_text())


class TestPipeline:
    """Fault-injection pipeline (SHIZZLE_PIPELINE=test) — test instrumentation only.

    Selecting it requires SHIZZLE_ALLOW_TEST_PIPELINE=1 (invariant C7): the
    stub produces no media, and a production container that ran it published
    a phantom library row on 2026-08-19.

    Effects are counted in <job_dir>/effects.log ("<job_dir_name> <stage>" per
    line, appended only when real work happens), and completion is marked with
    .<stage>.done files, mirroring how the real pipeline's on-disk outputs make
    re-runs idempotent. The log is per-job on purpose: only the lease holder
    works a job, so concurrent orchestrator processes never contend on the
    same file (a single shared log loses lines under concurrent appends on
    Windows). SHIZZLE_TEST_FAIL_TIMES injects N retryable failures in split;
    SHIZZLE_TEST_STAGE_SLEEP holds split open so tests can kill the process
    mid-stage.
    """

    SUBSTAGES = ("extracting", "splitting", "encoding")

    def __init__(self, settings: Settings) -> None:
        self._sleep = settings.shizzle_test_stage_sleep
        self._fail_times = settings.shizzle_test_fail_times

    def _record_effect(self, job_dir: Path, stage: str) -> None:
        job_dir.mkdir(parents=True, exist_ok=True)
        with open(job_dir / "effects.log", "a", encoding="utf-8") as f:
            f.write(f"{job_dir.name} {stage}\n")

    async def split(self, job_dir: Path, source_path: Path, title: str) -> dict[str, float]:
        if self._fail_times > 0:
            counter = job_dir / ".fail_count"
            failed_so_far = int(counter.read_text()) if counter.exists() else 0
            if failed_so_far < self._fail_times:
                counter.write_text(str(failed_so_far + 1))
                raise StageError(
                    ErrorCode.DEMUCS_FAILED,
                    f"injected failure {failed_so_far + 1}/{self._fail_times}",
                )
        timings: dict[str, float] = {}
        for sub in self.SUBSTAGES:
            marker = job_dir / f".{sub}.done"
            if marker.exists():
                continue  # completed before a crash — do not redo the effect
            if self._sleep:
                await asyncio.sleep(self._sleep)
            self._record_effect(job_dir, sub)
            marker.write_text("done")
            timings[sub] = self._sleep
        manifest = {
            "version": 3,
            "title": title,
            "artist": "",
            "duration": 1.0,
            "video": "video.mp4",
            "stems": [],
            "test_pipeline": True,
            "source": source_path.name,
        }
        (job_dir / "stems.json").write_text(json.dumps(manifest))
        return timings

    async def verify(self, job_dir: Path) -> dict[str, Any]:
        if not (job_dir / "stems.json").exists():
            raise StageError(
                ErrorCode.CHECKSUM_MISMATCH, "stems.json missing", retryable=False
            )
        self._record_effect(job_dir, "verify")
        return {"test_pipeline": True}

    async def describe(self, job_dir: Path) -> dict[str, Any]:
        return json.loads((job_dir / "stems.json").read_text())


def build_pipeline(settings: Settings) -> Pipeline:
    if settings.shizzle_pipeline == "test":
        if not settings.shizzle_allow_test_pipeline:
            raise RuntimeError(
                "SHIZZLE_PIPELINE=test requires SHIZZLE_ALLOW_TEST_PIPELINE=1: "
                "the test pipeline is a stub that produces no media and must "
                "never run in normal operation (invariant C7)"
            )
        return TestPipeline(settings)
    return LocalPipeline()
