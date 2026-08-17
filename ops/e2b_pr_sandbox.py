#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "e2b==2.35.0",
# ]
# ///
"""Host-side controller for isolated E2B pull-request workspaces.

The controller deliberately keeps GitHub write credentials on the host. Work
returns from a writer sandbox as a verified git bundle. The optional host-side
push command uses an argument-array refspec and verifies the PR head afterward.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = ROOT / ".sandbox" / "e2b"
RUNS_DIR = STATE_ROOT / "runs"
ARTIFACTS_DIR = STATE_ROOT / "artifacts"
DEFAULT_TEMPLATE = "pr-review-v1"
ACTIVE_STATES = {
    "planned",
    "provisioning",
    "ready",
    "running",
    "paused",
    "paused-failed",
}
ROLE_RE = re.compile(r"^(writer|audit(?:-[a-z0-9-]+)?|test(?:-[a-z0-9-]+)?)$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ControllerError(RuntimeError):
    """A user-actionable controller failure."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_run_id(role: str, pr_number: int) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_role = re.sub(r"[^a-z0-9-]", "-", role.lower())
    return f"pr{pr_number}-{safe_role}-{stamp}-{uuid.uuid4().hex[:8]}"


def validate_role(role: str) -> str:
    if not ROLE_RE.fullmatch(role):
        raise ControllerError("role must be writer, audit[-name], or test[-name]")
    return role


def validate_repo(repo: str) -> str:
    if not REPO_RE.fullmatch(repo):
        raise ControllerError("repository must be in owner/name form")
    return repo


def run_path(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ControllerError("invalid run ID")
    return RUNS_DIR / f"{run_id}.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def load_record(run_id: str) -> dict[str, Any]:
    path = run_path(run_id)
    if not path.exists():
        raise ControllerError(f"unknown run ID: {run_id}")
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def save_record(record: dict[str, Any]) -> None:
    record["updated_at"] = utc_now()
    atomic_write_json(run_path(str(record["run_id"])), record)


def list_records() -> list[dict[str, Any]]:
    if not RUNS_DIR.exists():
        return []
    records = []
    for path in sorted(RUNS_DIR.glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return records


def ensure_writer_available(repo: str, pr_number: int, role: str) -> None:
    if role != "writer":
        return
    conflicts = [
        record["run_id"]
        for record in list_records()
        if record.get("repo") == repo
        and record.get("pr_number") == pr_number
        and record.get("role") == "writer"
        and record.get("state") in ACTIVE_STATES
    ]
    if conflicts:
        raise ControllerError(
            "an active writer already exists for this PR: " + ", ".join(conflicts)
        )


def host_command(
    args: list[str], *, cwd: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise ControllerError(
            f"required host command is unavailable: {args[0]}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise ControllerError(f"host command failed ({args[0]}): {detail}") from exc


def resolve_pull_request(repo: str, pr_number: int) -> dict[str, Any]:
    validate_repo(repo)
    result = host_command(["gh", "api", f"repos/{repo}/pulls/{pr_number}"])
    payload = json.loads(result.stdout)
    return {
        "url": payload["html_url"],
        "repo": payload["base"]["repo"]["full_name"],
        "pr_number": int(payload["number"]),
        "base_ref": payload["base"]["ref"],
        "base_sha": payload["base"]["sha"],
        "head_repo": payload["head"]["repo"]["full_name"],
        "head_ref": payload["head"]["ref"],
        "head_sha": payload["head"]["sha"],
        "draft": bool(payload["draft"]),
        "state": payload["state"],
    }


def require_e2b() -> Any:
    if not os.environ.get("E2B_API_KEY"):
        raise ControllerError("E2B_API_KEY is not present in the host environment")
    try:
        from e2b import Sandbox
    except ImportError as exc:
        raise ControllerError("the E2B SDK could not be imported") from exc
    return Sandbox


def remote_run(sandbox: Any, script: str, *, timeout: int = 900) -> Any:
    try:
        result = sandbox.commands.run(
            f"bash -lc {shlex.quote(script)}",
            timeout=timeout,
        )
    except Exception as exc:
        stdout = str(getattr(exc, "stdout", "") or "").strip()
        stderr = str(getattr(exc, "stderr", "") or "").strip()
        detail = "\n".join(part for part in (stdout, stderr) if part)
        raise ControllerError(detail or str(exc)) from exc
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout or "remote command failed").strip()
        raise ControllerError(detail)
    return result


def connect(record: dict[str, Any], *, timeout: int = 900) -> Any:
    sandbox_id = record.get("sandbox_id")
    if not sandbox_id:
        raise ControllerError("run has no E2B sandbox ID")
    Sandbox = require_e2b()
    return Sandbox.connect(sandbox_id, timeout=timeout)


def initial_record(
    pull: dict[str, Any], role: str, template: str, timeout: int, run_id: str
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": 1,
        "run_id": run_id,
        "sandbox_id": None,
        "provider": "e2b",
        "controller_sdk": f"e2b=={version('e2b')}",
        "template": template,
        "role": role,
        "repo": pull["repo"],
        "pr_number": pull["pr_number"],
        "pr_url": pull["url"],
        "base_ref": pull["base_ref"],
        "base_sha": pull["base_sha"],
        "head_repo": pull["head_repo"],
        "head_ref": pull["head_ref"],
        "head_sha": pull["head_sha"],
        "pr_state": pull.get("state", "open"),
        "checkout_path": "/workspace/repo",
        "branch": f"sandbox/{run_id}",
        "timeout_seconds": timeout,
        "lifecycle": {"on_timeout": "pause", "auto_resume": True},
        "state": "planned",
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
        "setup": None,
        "artifacts": [],
        "last_error": None,
    }


def provision(
    repo: str,
    pr_number: int,
    role: str,
    template: str,
    timeout: int,
    setup: str,
    setup_file: str | None,
    keep_running: bool,
) -> dict[str, Any]:
    role = validate_role(role)
    custom_setup = Path(setup_file).resolve() if setup_file else None
    if custom_setup is not None and not custom_setup.is_file():
        raise ControllerError(f"setup file does not exist: {custom_setup}")
    pull = resolve_pull_request(repo, pr_number)
    ensure_writer_available(pull["repo"], pr_number, role)
    Sandbox = require_e2b()
    run_id = new_run_id(role, pr_number)
    record = initial_record(pull, role, template, timeout, run_id)
    save_record(record)  # Durable intent exists before the remote resource.

    metadata = {
        "managed-by": "e2b-pr-review-controller",
        "run-id": run_id,
        "repo": pull["repo"],
        "pr": str(pr_number),
        "role": role,
        "head-sha": pull["head_sha"],
    }
    sandbox = None
    try:
        record["state"] = "provisioning"
        save_record(record)
        sandbox = Sandbox.create(
            template=template,
            timeout=timeout,
            metadata=metadata,
            lifecycle={"on_timeout": "pause", "auto_resume": True},
        )
        record["sandbox_id"] = sandbox.sandbox_id
        save_record(record)

        clone_url = f"https://github.com/{pull['repo']}.git"
        checkout = record["checkout_path"]
        branch = record["branch"]
        clone_script = "\n".join(
            [
                "set -euo pipefail",
                f"git clone --filter=blob:none {shlex.quote(clone_url)} {shlex.quote(checkout)}",
                (
                    f"git -C {shlex.quote(checkout)} fetch origin "
                    f"+refs/pull/{pr_number}/head:refs/remotes/origin/pr/{pr_number}"
                ),
                (
                    f"git -C {shlex.quote(checkout)} checkout -b {shlex.quote(branch)} "
                    f"{shlex.quote(pull['head_sha'])}"
                ),
                (
                    f'test "$(git -C {shlex.quote(checkout)} rev-parse HEAD)" = '
                    f"{shlex.quote(pull['head_sha'])}"
                ),
                f'test -z "$(git -C {shlex.quote(checkout)} status --porcelain)"',
                "git --version",
                "uv --version",
                "node --version",
                "npm --version",
            ]
        )
        result = remote_run(sandbox, clone_script, timeout=timeout)
        setup_mode = "file" if custom_setup is not None else setup
        record["setup"] = {
            "mode": setup_mode,
            "bootstrap_stdout": result.stdout.strip(),
        }

        setup_path = ROOT / "ops" / "e2b_shizzle_setup.sh"
        should_setup = setup == "shizzle" or (
            setup == "auto" and pull["repo"].lower().endswith("/shizzle")
        )
        if custom_setup is not None:
            setup_data = custom_setup.read_bytes()
            remote_setup = "/tmp/e2b_project_setup.sh"
            sandbox.files.write(remote_setup, setup_data.decode("utf-8"))
            setup_result = remote_run(
                sandbox,
                f"bash {remote_setup} {shlex.quote(checkout)}",
                timeout=timeout,
            )
            record["setup"].update(
                {
                    "source": str(custom_setup),
                    "sha256": hashlib.sha256(setup_data).hexdigest(),
                    "project_stdout": setup_result.stdout.strip(),
                }
            )
        elif should_setup:
            sandbox.files.write(
                "/tmp/e2b_shizzle_setup.sh", setup_path.read_text(encoding="utf-8")
            )
            setup_result = remote_run(
                sandbox,
                f"bash /tmp/e2b_shizzle_setup.sh {shlex.quote(checkout)}",
                timeout=timeout,
            )
            record["setup"]["project_stdout"] = setup_result.stdout.strip()
        elif setup not in {"auto", "none"}:
            raise ControllerError("setup must be auto, shizzle, or none")

        record["state"] = "running"
        save_record(record)
        if not keep_running:
            sandbox.pause(keep_memory=True)
            record["state"] = "paused"
            save_record(record)
        return record
    except BaseException as exc:
        record["state"] = "failed"
        record["last_error"] = str(exc)
        save_record(record)
        if sandbox is not None:
            try:
                sandbox.pause(keep_memory=True)
                record["state"] = "paused-failed"
                save_record(record)
            except Exception as pause_exc:  # noqa: BLE001 - preserve the provisioning failure
                record["pause_error"] = str(pause_exc)
                save_record(record)
        raise


def command_doctor(_: argparse.Namespace) -> None:
    Sandbox = require_e2b()
    gh = host_command(["gh", "--version"]).stdout.splitlines()[0]
    observed = Sandbox.list(limit=1).next_items()
    print(f"E2B SDK: {version('e2b')}")
    print(f"GitHub CLI: {gh}")
    print(f"E2B API: reachable (observed {len(observed)} sandbox(es) in probe)")


def command_create(args: argparse.Namespace) -> None:
    record = provision(
        args.repo,
        args.pr,
        args.role,
        args.template,
        args.timeout,
        args.setup,
        args.setup_file,
        args.keep_running,
    )
    print(json.dumps(record, indent=2, sort_keys=True))


def command_fanout(args: argparse.Namespace) -> None:
    roles = [
        validate_role(item.strip()) for item in args.roles.split(",") if item.strip()
    ]
    if not roles:
        raise ControllerError("fanout needs at least one role")
    if roles.count("writer") > 1:
        raise ControllerError("fanout permits at most one writer")
    created = []
    for role in roles:
        created.append(
            provision(
                args.repo,
                args.pr,
                role,
                args.template,
                args.timeout,
                args.setup,
                args.setup_file,
                False,
            )
        )
    print(json.dumps(created, indent=2, sort_keys=True))


def command_list(_: argparse.Namespace) -> None:
    rows = [
        {
            "run_id": record.get("run_id"),
            "sandbox_id": record.get("sandbox_id"),
            "repo": record.get("repo"),
            "pr": record.get("pr_number"),
            "role": record.get("role"),
            "head_sha": record.get("head_sha"),
            "state": record.get("state"),
        }
        for record in list_records()
    ]
    print(json.dumps(rows, indent=2, sort_keys=True))


def command_info(args: argparse.Namespace) -> None:
    record = load_record(args.run_id)
    if args.refresh and record.get("sandbox_id"):
        prior_state = record.get("state")
        sandbox = connect(record, timeout=60)
        info = sandbox.get_info()
        record["provider_info"] = {
            "sandbox_id": info.sandbox_id,
            "template_id": info.template_id,
            "started_at": str(info.started_at),
            "end_at": str(info.end_at),
        }
        if prior_state in {"paused", "paused-failed"}:
            sandbox.pause(keep_memory=True)
            record["state"] = prior_state
        else:
            record["state"] = "running"
        save_record(record)
    print(json.dumps(record, indent=2, sort_keys=True))


def command_exec(args: argparse.Namespace) -> None:
    if not args.command:
        raise ControllerError("exec requires a command after --")
    record = load_record(args.run_id)
    sandbox = connect(record, timeout=args.timeout)
    record["state"] = "running"
    save_record(record)
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    try:
        result = sandbox.commands.run(
            shlex.join(cmd),
            cwd=record["checkout_path"],
            timeout=args.timeout,
        )
    except Exception as exc:
        stdout = str(getattr(exc, "stdout", "") or "")
        stderr = str(getattr(exc, "stderr", "") or "")
        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
        exit_code = getattr(exc, "exit_code", "unknown")
        raise ControllerError(f"remote command exited {exit_code}") from exc
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.exit_code != 0:
        raise ControllerError(f"remote command exited {result.exit_code}")


def command_apply_patch(args: argparse.Namespace) -> None:
    record = load_record(args.run_id)
    patch_path = Path(args.patch_file).resolve()
    if not patch_path.is_file():
        raise ControllerError(f"patch file does not exist: {patch_path}")
    patch_data = patch_path.read_text(encoding="utf-8")
    if not patch_data.strip():
        raise ControllerError("patch file is empty")
    sandbox = connect(record, timeout=args.timeout)
    record["state"] = "running"
    save_record(record)
    remote_patch = f"/tmp/{record['run_id']}-{uuid.uuid4().hex[:8]}.patch"
    sandbox.files.write(remote_patch, patch_data)
    result = remote_run(
        sandbox,
        "\n".join(
            [
                "set -euo pipefail",
                (
                    f"git -C {shlex.quote(record['checkout_path'])} apply --check "
                    f"{shlex.quote(remote_patch)}"
                ),
                (
                    f"git -C {shlex.quote(record['checkout_path'])} apply "
                    f"{shlex.quote(remote_patch)}"
                ),
                f"git -C {shlex.quote(record['checkout_path'])} diff --check",
                f"rm -f {shlex.quote(remote_patch)}",
            ]
        ),
        timeout=args.timeout,
    )
    print(result.stdout, end="")
    print(f"applied {patch_path.name} to {args.run_id}")


def command_sync_diff(args: argparse.Namespace) -> None:
    record = load_record(args.run_id)
    source = Path(args.source_worktree).resolve()
    if args.replace and not args.base_ref:
        raise ControllerError("--replace requires --base-ref")
    staging_root = (STATE_ROOT / "staging").resolve()
    if not source.is_relative_to(staging_root):
        raise ControllerError(f"source worktree must be beneath {staging_root}")
    if not (source / ".git").exists():
        raise ControllerError(f"source is not a Git worktree: {source}")
    source_head = host_command(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    patch_base = record["head_sha"]
    if args.base_ref:
        patch_base = host_command(
            ["git", "rev-parse", "--verify", args.base_ref], cwd=source
        ).stdout.strip()
        host_command(
            ["git", "merge-base", "--is-ancestor", source_head, patch_base],
            cwd=source,
        )
    elif source_head != record["head_sha"]:
        raise ControllerError(
            f"source HEAD {source_head} does not match recorded PR head {record['head_sha']}"
        )
    host_command(["git", "add", "-N", "--", "."], cwd=source)
    host_command(["git", "diff", "--check"], cwd=source)
    patch_data = host_command(
        ["git", "diff", "--binary", "--no-ext-diff", patch_base], cwd=source
    ).stdout
    if not patch_data.strip():
        raise ControllerError("source worktree has no diff to synchronize")

    sandbox = connect(record, timeout=args.timeout)
    record["state"] = "running"
    save_record(record)
    remote_patch = f"/tmp/{record['run_id']}-{uuid.uuid4().hex[:8]}.patch"
    sandbox.files.write(remote_patch, patch_data)
    remote_prep = [
        (
            f"test \"$(git -C {shlex.quote(record['checkout_path'])} "
            f"rev-parse HEAD)\" = {shlex.quote(patch_base)}"
        )
    ]
    if not args.base_ref or args.replace:
        remote_prep.extend(
            [
                f"git -C {shlex.quote(record['checkout_path'])} reset --hard HEAD",
                f"git -C {shlex.quote(record['checkout_path'])} clean -fd",
            ]
        )
    result = remote_run(
        sandbox,
        "\n".join(
            [
                "set -euo pipefail",
                *remote_prep,
                (
                    f"git -C {shlex.quote(record['checkout_path'])} apply --check "
                    f"{shlex.quote(remote_patch)}"
                ),
                (
                    f"git -C {shlex.quote(record['checkout_path'])} apply "
                    f"{shlex.quote(remote_patch)}"
                ),
                f"git -C {shlex.quote(record['checkout_path'])} diff --check",
                f"rm -f {shlex.quote(remote_patch)}",
            ]
        ),
        timeout=args.timeout,
    )
    print(result.stdout, end="")
    print(f"synchronized {len(patch_data.encode('utf-8'))} patch bytes to {args.run_id}")


def command_pause(args: argparse.Namespace) -> None:
    record = load_record(args.run_id)
    sandbox = connect(record, timeout=60)
    sandbox.pause(keep_memory=True)
    record["state"] = "paused"
    save_record(record)
    print(f"paused {args.run_id} ({record['sandbox_id']})")


def command_resume(args: argparse.Namespace) -> None:
    record = load_record(args.run_id)
    sandbox = connect(record, timeout=args.timeout)
    info = sandbox.get_info()
    record["state"] = "running"
    save_record(record)
    print(f"running {args.run_id} ({info.sandbox_id})")


def command_harvest(args: argparse.Namespace) -> None:
    record = load_record(args.run_id)
    if record["role"] != "writer":
        raise ControllerError("only a writer run may produce an importable git bundle")
    sandbox = connect(record, timeout=args.timeout)
    checkout = record["checkout_path"]
    base_sha = record["base_sha"]
    status = remote_run(
        sandbox,
        "\n".join(
            [
                "set -euo pipefail",
                (
                    f'test -z "$(git -C {shlex.quote(checkout)} status --porcelain)" || '
                    "{ echo 'commit or remove all dirty and untracked files before harvest' "
                    ">&2; exit 2; }"
                ),
                (
                    f'test "$(git -C {shlex.quote(checkout)} rev-list --count '
                    f'{shlex.quote(base_sha)}..HEAD)" -gt 0 || '
                    "{ echo 'no commits exist beyond the recorded base SHA' >&2; exit 3; }"
                ),
                f"git -C {shlex.quote(checkout)} rev-parse HEAD",
            ]
        ),
        timeout=args.timeout,
    )
    harvested_sha = status.stdout.strip().splitlines()[-1]
    pull = resolve_pull_request(record["repo"], record["pr_number"])
    expected_remote_head = pull["head_sha"]
    remote_bundle = f"/tmp/{record['run_id']}.bundle"
    remote_run(
        sandbox,
        f"git -C {shlex.quote(checkout)} bundle create {shlex.quote(remote_bundle)} "
        f"HEAD ^{shlex.quote(base_sha)}",
        timeout=args.timeout,
    )
    bundle_data = sandbox.files.read(remote_bundle, format="bytes")
    if not isinstance(bundle_data, bytes):
        bundle_data = bytes(bundle_data)
    artifact = ARTIFACTS_DIR / f"{record['run_id']}.bundle"
    atomic_write_bytes(artifact, bundle_data)
    host_command(["git", "bundle", "verify", str(artifact)])

    imported_ref = None
    if not args.no_import:
        imported_ref = f"refs/sandbox/e2b/{record['run_id']}"
        host_command(["git", "fetch", str(artifact), f"HEAD:{imported_ref}"])
        imported_sha = host_command(["git", "rev-parse", imported_ref]).stdout.strip()
        if imported_sha != harvested_sha:
            raise ControllerError("imported bundle SHA does not match sandbox HEAD")
        host_command(
            ["git", "merge-base", "--is-ancestor", expected_remote_head, imported_ref]
        )

    record["artifacts"].append(
        {
            "kind": "git-bundle",
            "path": str(artifact.relative_to(ROOT)),
            "sha": harvested_sha,
            "expected_remote_head": expected_remote_head,
            "imported_ref": imported_ref,
            "verified_at": utc_now(),
        }
    )
    save_record(record)
    print(json.dumps(record["artifacts"][-1], indent=2, sort_keys=True))


def command_push(args: argparse.Namespace) -> None:
    record = load_record(args.run_id)
    if record["role"] != "writer":
        raise ControllerError("only a writer run may push a harvested artifact")
    if not record["artifacts"]:
        raise ControllerError("run has no harvested artifact")
    artifact = record["artifacts"][-1]
    imported_ref = artifact.get("imported_ref")
    expected_head = artifact.get("expected_remote_head")
    if not imported_ref or not expected_head:
        raise ControllerError("latest artifact lacks an imported ref or expected remote head")
    pull = resolve_pull_request(record["repo"], record["pr_number"])
    if pull["state"] != "open":
        raise ControllerError(f"pull request is {pull['state']}; refusing to push")
    if pull["head_repo"] != pull["repo"]:
        raise ControllerError("fork-head pushes require an explicit host remote workflow")
    destination = f"refs/heads/{pull['head_ref']}"
    host_command(["git", "check-ref-format", destination])
    if pull["head_sha"] == artifact["sha"]:
        # Recover idempotently when Git accepted the prior push but the
        # provider's first PR-head reads lagged past the verification window.
        artifact.setdefault("pushed_at", utc_now())
        artifact["reconciled_at"] = utc_now()
        artifact["remote"] = args.remote
        artifact["destination"] = destination
        save_record(record)
        print(f"already at {artifact['sha']} on {args.remote} {destination}")
        return
    if pull["head_sha"] != expected_head:
        raise ControllerError(
            f"remote head advanced from {expected_head} to {pull['head_sha']}"
        )
    host_command(["git", "push", args.remote, f"{imported_ref}:{destination}"])
    observed = None
    post_push_attempts = 0
    # Git's receive-pack result is authoritative for the write, while the PR
    # GraphQL view can lag briefly behind the branch ref. Retry only this
    # read-after-write verification; never retry the push itself.
    for delay in (0, 1, 2, 4, 8, 8):
        if delay:
            time.sleep(delay)
        post_push_attempts += 1
        observed = resolve_pull_request(record["repo"], record["pr_number"])
        if observed["head_sha"] == artifact["sha"]:
            break
    if observed is None or observed["head_sha"] != artifact["sha"]:
        raise ControllerError(
            f"push completed but PR head is {observed['head_sha']}, not {artifact['sha']}"
        )
    artifact["pushed_at"] = utc_now()
    artifact["post_push_attempts"] = post_push_attempts
    artifact["remote"] = args.remote
    artifact["destination"] = destination
    save_record(record)
    print(f"pushed {artifact['sha']} to {args.remote} {destination}")


def command_destroy(args: argparse.Namespace) -> None:
    record = load_record(args.run_id)
    if args.confirm != args.run_id:
        raise ControllerError("destruction requires --confirm with the exact run ID")
    sandbox = connect(record, timeout=60)
    sandbox.kill()
    record["state"] = "destroyed"
    record["closed_at"] = utc_now()
    save_record(record)
    print(f"destroyed {args.run_id} ({record['sandbox_id']}); local record retained")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="subcommand", required=True)

    doctor = commands.add_parser("doctor", help="check host and E2B connectivity")
    doctor.set_defaults(func=command_doctor)

    create = commands.add_parser("create", help="create and pin one PR sandbox")
    create.add_argument("--repo", required=True)
    create.add_argument("--pr", required=True, type=int)
    create.add_argument("--role", default="writer")
    create.add_argument("--template", default=DEFAULT_TEMPLATE)
    create.add_argument("--timeout", type=int, default=1800)
    create.add_argument("--setup", choices=["auto", "shizzle", "none"], default="auto")
    create.add_argument(
        "--setup-file",
        help="host setup script to upload and run as: bash SCRIPT CHECKOUT_PATH",
    )
    create.add_argument("--keep-running", action="store_true")
    create.set_defaults(func=command_create)

    fanout = commands.add_parser(
        "fanout", help="create separate writer/audit/test lanes"
    )
    fanout.add_argument("--repo", required=True)
    fanout.add_argument("--pr", required=True, type=int)
    fanout.add_argument(
        "--roles", required=True, help="comma-separated roles; at most one writer"
    )
    fanout.add_argument("--template", default=DEFAULT_TEMPLATE)
    fanout.add_argument("--timeout", type=int, default=1800)
    fanout.add_argument("--setup", choices=["auto", "shizzle", "none"], default="auto")
    fanout.add_argument(
        "--setup-file",
        help="host setup script to upload and run in every lane",
    )
    fanout.set_defaults(func=command_fanout)

    listing = commands.add_parser("list", help="list controller-side run records")
    listing.set_defaults(func=command_list)

    info = commands.add_parser("info", help="show one run record")
    info.add_argument("run_id")
    info.add_argument("--refresh", action="store_true")
    info.set_defaults(func=command_info)

    execute = commands.add_parser("exec", help="run a command in a sandbox checkout")
    execute.add_argument("run_id")
    execute.add_argument("--timeout", type=int, default=1800)
    execute.add_argument("command", nargs=argparse.REMAINDER)
    execute.set_defaults(func=command_exec)

    apply_patch_parser = commands.add_parser(
        "apply-patch", help="verify and apply a host patch in a sandbox lane"
    )
    apply_patch_parser.add_argument("run_id")
    apply_patch_parser.add_argument("patch_file")
    apply_patch_parser.add_argument("--timeout", type=int, default=1800)
    apply_patch_parser.set_defaults(func=command_apply_patch)

    sync_diff = commands.add_parser(
        "sync-diff", help="apply a validated ignored staging-worktree diff in E2B"
    )
    sync_diff.add_argument("run_id")
    sync_diff.add_argument("source_worktree")
    sync_diff.add_argument(
        "--base-ref",
        help="generate an incremental patch from this host ref and require the lane at it",
    )
    sync_diff.add_argument(
        "--replace",
        action="store_true",
        help="reset a disposable lane at --base-ref before replaying a revised candidate",
    )
    sync_diff.add_argument("--timeout", type=int, default=1800)
    sync_diff.set_defaults(func=command_sync_diff)

    pause = commands.add_parser("pause", help="pause a sandbox and preserve state")
    pause.add_argument("run_id")
    pause.set_defaults(func=command_pause)

    resume = commands.add_parser("resume", help="resume/connect to a sandbox")
    resume.add_argument("run_id")
    resume.add_argument("--timeout", type=int, default=1800)
    resume.set_defaults(func=command_resume)

    harvest = commands.add_parser(
        "harvest", help="verify and import a writer git bundle"
    )
    harvest.add_argument("run_id")
    harvest.add_argument("--timeout", type=int, default=1800)
    harvest.add_argument("--no-import", action="store_true")
    harvest.set_defaults(func=command_harvest)

    push = commands.add_parser(
        "push", help="push the latest verified writer artifact after a head check"
    )
    push.add_argument("run_id")
    push.add_argument("--remote", default="origin")
    push.set_defaults(func=command_push)

    destroy = commands.add_parser(
        "destroy", help="permanently destroy a remote sandbox"
    )
    destroy.add_argument("run_id")
    destroy.add_argument("--confirm", required=True)
    destroy.set_defaults(func=command_destroy)
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        args = build_parser().parse_args()
        args.func(args)
        return 0
    except (ControllerError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - provider/CLI boundary
        print(f"provider error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
