"""The agent loop: heartbeat, long-poll, execute one assignment at a time.

Reports are hints; the orchestrator trusts S3 markers (``source.json``,
``handoff.json``). So the agent's job is to do the work, write the markers
through the production code paths, and report truthfully. A crash mid-job
leaves no marker, the orchestrator expires the assignment after its stall
window and redispatches under a fresh key (B12).
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .client import ApiError, Assignment, NodeClient
from .config import NodeConfig
from .download import DownloadError, build_receipt, download_source, upload_source
from .separate import resolve_device, run_separation

logger = logging.getLogger("shizzle_node")


class Agent:
    def __init__(
        self,
        config: NodeConfig,
        client: NodeClient,
        *,
        s3_factory: Callable[[], Any],
        downloader: Callable[..., Any] = download_source,
        uploader: Callable[..., None] = upload_source,
        separator: Callable[..., dict[str, Any]] = run_separation,
        device_resolver: Callable[[str], str] = resolve_device,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.client = client
        self._s3_factory = s3_factory
        self._download = downloader
        self._upload = uploader
        self._separate = separator
        self._device = device_resolver(config.device) if "separate" in config.capabilities else "none"
        self._sleep = sleep
        self._busy: uuid.UUID | None = None
        self._stop = threading.Event()

    # --- lifecycle -------------------------------------------------------------

    @property
    def device(self) -> str:
        return self._device

    def worker_image(self) -> str:
        return f"node:{self.config.node_id}/shizzle-node-{self.config.agent_version}/{self._device}"

    def request_stop(self) -> None:
        self._stop.set()

    def heartbeat_once(self) -> bool:
        try:
            self.client.heartbeat(
                display_name=self.config.display_name,
                capabilities=self.config.capability_payload(resolved_device=self._device),
                agent_version=self.config.agent_version,
                busy_assignment_id=self._busy,
            )
            return True
        except Exception as exc:
            logger.warning("heartbeat failed: %s", exc)
            return False

    def _heartbeat_forever(self) -> None:
        while not self._stop.is_set():
            self.heartbeat_once()
            self._stop.wait(self.config.heartbeat_seconds)

    def run(self) -> None:
        """Block until stopped: one heartbeat thread, one assignment at a time."""
        logger.info(
            "shizzle-node %s (%s) -> %s, device=%s, capabilities=%s",
            self.config.node_id,
            self.config.display_name,
            self.config.api_base,
            self._device,
            ",".join(sorted(self.config.capabilities)),
        )
        beat = threading.Thread(target=self._heartbeat_forever, name="heartbeat", daemon=True)
        beat.start()
        backoff = 2.0
        while not self._stop.is_set():
            try:
                assignment = self.client.next_assignment(wait_seconds=self.config.poll_wait_seconds)
            except Exception as exc:
                logger.warning("poll failed: %s; retrying in %.0fs", exc, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            backoff = 2.0
            if assignment is None:
                continue
            self.execute(assignment)
        self._stop.set()
        beat.join(timeout=5)

    # --- one assignment ------------------------------------------------------

    def execute(self, assignment: Assignment) -> None:
        if assignment.kind not in self.config.capabilities:
            self._fail(assignment, "UNSUPPORTED_KIND", f"this node does not do {assignment.kind}")
            return
        try:
            self.client.ack(assignment.id)
        except ApiError as exc:
            # 409: the orchestrator expired or reassigned it; nothing to do.
            logger.info("assignment %s not acknowledged (%s); skipping", assignment.id, exc)
            return
        self._busy = assignment.id
        self.heartbeat_once()
        workdir = self.config.workdir / assignment.job_id / assignment.kind
        last = [""]

        def progress(msg: str) -> None:
            if msg == last[0]:
                return
            last[0] = msg
            logger.info("[%s] %s", assignment.id, msg)
            try:
                self.client.progress(assignment.id, msg)
            except Exception as exc:  # progress is best-effort
                logger.debug("progress report failed: %s", exc)

        try:
            if assignment.kind == "download":
                detail = self._do_download(assignment, workdir, progress)
            elif assignment.kind == "separate":
                detail = self._do_separate(assignment, workdir, progress)
            else:
                raise RuntimeError(f"unknown assignment kind {assignment.kind}")
        except DownloadError as exc:
            self._fail(assignment, exc.code, exc.detail)
        except Exception as exc:
            code = exc.__class__.__name__[:64]
            if code == "AttemptClaimedError":
                code = "ATTEMPT_CLAIMED"
            self._fail(assignment, code, str(exc)[:1500])
        else:
            try:
                self.client.complete(assignment.id, detail)
                logger.info("assignment %s complete: %s", assignment.id, detail)
            except Exception as exc:
                # The S3 markers are written; the orchestrator will see them.
                logger.warning("completion report failed for %s: %s", assignment.id, exc)
            shutil.rmtree(workdir, ignore_errors=True)
        finally:
            self._busy = None
            self.heartbeat_once()

    def _fail(self, assignment: Assignment, code: str, detail: str) -> None:
        logger.error("assignment %s failed: %s %s", assignment.id, code, detail)
        try:
            self.client.fail(assignment.id, code=code, detail=detail)
        except Exception as exc:
            logger.warning("failure report failed for %s: %s", assignment.id, exc)

    def _do_download(
        self, assignment: Assignment, workdir: Path, progress: Callable[[str], None]
    ) -> dict[str, Any]:
        payload = assignment.payload
        for key in ("url", "track_id", "bucket", "source_key"):
            if not payload.get(key):
                raise DownloadError("BAD_PAYLOAD", f"assignment payload lacks {key}")
        source = self._download(
            str(payload["url"]),
            workdir,
            max_bytes=int(payload.get("max_bytes") or 2 * 1024**3),
            max_duration_seconds=float(payload.get("max_duration_seconds") or 1800),
            cookies_file=self.config.cookies_file,
            progress=progress,
        )
        receipt = build_receipt(source, payload=payload, node_id=self.config.node_id)
        self._upload(
            self._s3_factory(),
            bucket=str(payload["bucket"]),
            source=source,
            receipt=receipt,
            progress=progress,
        )
        return {
            "sha256": source.sha256,
            "bytes": source.bytes,
            "duration_seconds": round(source.duration_seconds, 3),
            "title": source.title,
            "uploader": source.uploader,
            "video_id": source.video_id,
        }

    def _do_separate(
        self, assignment: Assignment, workdir: Path, progress: Callable[[str], None]
    ) -> dict[str, Any]:
        payload = assignment.payload
        for key in ("track_id", "bucket", "input_key", "output_prefix", "idempotency_key"):
            if not payload.get(key):
                raise RuntimeError(f"assignment payload lacks {key}")
        workdir.mkdir(parents=True, exist_ok=True)
        result = self._separate(
            payload,
            executor_job_id=f"node:{assignment.id}",
            device=self._device,
            worker_image=self.worker_image(),
            stemsplit_dir=self.config.stemsplit_dir,
            progress=progress,
        )
        return {
            "status": result.get("status"),
            "package_prefix": result.get("package_prefix"),
            "sample_count": result.get("sample_count"),
            "uploads": result.get("uploads"),
            "timings": result.get("timings"),
            "device": self._device,
        }
