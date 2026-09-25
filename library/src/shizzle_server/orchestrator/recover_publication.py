"""Explicit publication recovery for a completed, already-submitted GPU job.

Run with --execute only after inspecting the default read-only preflight.
No path in this command submits or cancels a provider job.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from pathlib import Path

from ..db.models import JobStage
from ..db.repository import recoverable_publication_failure, track_id_for_job
from ..errors import ErrorCode, StageError
from ..publish.lossless_intake import load_and_verify_package
from .cloud import accepted_dispatch_id, attempt_prefix, package_ready, source_key
from .loop import Orchestrator
from .stages import StageContext, _confirmed_dispatch_key


def source_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


async def completion_evidence(ctx: StageContext, *, allow_receipt: bool) -> str:
    try:
        result = await ctx.runpod.poll(ctx.job.runpod_job_id)
    except StageError as error:
        if not (
            allow_receipt and error.code == ErrorCode.RUNPOD_DISPATCH_FAILED
            and error.detail == "RunPod returned HTTP 404"
        ):
            raise
        # Provider result retention can expire before publication is repaired.
        # A 404 alone never permits recovery: require durable completion and
        # the worker's matching, immutable dispatch receipt + handoff marker.
        events = await ctx.jobs.list_events(ctx.job.id)
        dispatch_key = _confirmed_dispatch_key(events, ctx.job.runpod_job_id)
        if not dispatch_key:
            raise ValueError("No confirmed dispatch reservation for this provider job") from None
        prefix = attempt_prefix(track_id_for_job(ctx.job.id), dispatch_key)
        selected = [event for event in events if event.event == "stage_completed"
                    and (event.detail or {}).get("from") == "dispatched"
                    and (event.detail or {}).get("to") == "verifying"]
        if (
            not any(event.event == "worker_completed" for event in events)
            or not selected or (selected[-1].detail or {}).get("package_prefix") != prefix
            or await accepted_dispatch_id(ctx, idempotency_key=dispatch_key) != ctx.job.runpod_job_id
            or not await package_ready(ctx, idempotency_key=dispatch_key)
        ):
            raise ValueError("Provider history is missing and completion receipts do not reconcile") from None
        return "durable_receipts_after_provider_404"
    if result.get("status") != "COMPLETED" or result.get("id") != ctx.job.runpod_job_id:
        raise ValueError("Provider completion is not confirmed; do not resubmit")
    return "live_provider"


async def recover(args: argparse.Namespace) -> dict:
    recovery_id = uuid.UUID(args.recovery_id)
    worker_id = "publication-recovery-" + recovery_id.hex + "-" + uuid.uuid4().hex
    orchestrator = Orchestrator(worker_id=worker_id)
    try:
        job = await orchestrator.jobs.get_job(uuid.UUID(args.job_id))
        if (
            not orchestrator.settings.cloud_pipeline or job is None
            or job.runpod_job_id != args.provider_job_id
            or job.input_checksum != args.input_checksum
            or job.status != JobStage.failed
        ):
            raise ValueError("Recovery requires the exact failed publication identity")
        events = await orchestrator.jobs.list_events(job.id)
        if not recoverable_publication_failure(job, events):
            raise ValueError("No recoverable publication failure provenance")
        ctx = StageContext(
            job=job, settings=orchestrator.settings, pipeline=orchestrator.pipeline,
            jobs=orchestrator.jobs, runpod=orchestrator.runpod, worker_id=worker_id,
        )
        completion = await completion_evidence(ctx, allow_receipt=args.use_completion_receipts)
        events = await orchestrator.jobs.list_events(job.id)
        dispatch_key = _confirmed_dispatch_key(events, job.runpod_job_id)
        if not dispatch_key:
            raise ValueError("No confirmed dispatch reservation for this provider job")
        # Verify existing bytes without altering the terminal job or package.
        package = await asyncio.to_thread(load_and_verify_package, ctx.job_dir / "package")
        source = ctx.source_path if ctx.source_path.is_file() else ctx.job_dir / "source.mp4"
        source_hash = await asyncio.to_thread(source_digest, source)
        source_info = package.handoff.get("source", {})
        if (
            source_hash != args.input_checksum
            or source_info.get("sha256") != args.input_checksum
            or source_info.get("object_key") != source_key(track_id_for_job(job.id))
        ):
            raise ValueError("Recovery source identity does not match verified bytes")
        report = {
            "job_id": str(job.id), "provider_job_id": job.runpod_job_id,
            "recovery_id": str(recovery_id), "input_checksum": source_hash,
            "completion_evidence": completion, "package_verified": True,
            "executed": bool(args.execute),
        }
        if not args.execute:
            return report
        claimed = await orchestrator.jobs.recover_publication(
            job.id, expected_provider_id=args.provider_job_id,
            expected_checksum=args.input_checksum,
            expected_idempotency_key=job.idempotency_key,
            expected_dispatch_key=dispatch_key,
            recovery_id=recovery_id, worker_id=worker_id,
            lease_seconds=orchestrator.settings.orchestrator_lease_seconds,
        )
        # Existing loop renews the lease, verifies the selected attempt again,
        # and atomically publishes ready. It starts after the dispatch stage.
        await orchestrator.process_job(claimed)
        current = await orchestrator.jobs.get_job(job.id)
        report["status"] = current.status.value
        report["published"] = current.status == JobStage.ready
        return report
    finally:
        await orchestrator.engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--provider-job-id", required=True)
    parser.add_argument("--input-checksum", required=True)
    parser.add_argument("--recovery-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--use-completion-receipts", action="store_true",
                        help="On provider HTTP 404 only, reconcile durable completion and worker receipts")
    try:
        report = asyncio.run(recover(parser.parse_args()))
    except Exception as exc:
        # Provider/configuration exception strings can contain sensitive data.
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(report))
    return 0 if not report["executed"] or report.get("published") else 1


if __name__ == "__main__":
    raise SystemExit(main())
