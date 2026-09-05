"""Tests for ops/migrate_and_audit_delivery_library.py child environment propagation.

The coordinator must resolve the database URL once and hand it to every
subprocess via a controlled child environment, never via argv.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "migrate_and_audit_delivery_library.py"
SPEC = importlib.util.spec_from_file_location(
    "migrate_and_audit_delivery_library", MODULE_PATH
)
assert SPEC and SPEC.loader
coordinator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coordinator
SPEC.loader.exec_module(coordinator)

TRACK_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")

EXPLICIT = "postgresql+asyncpg://explicit"
AMBIENT = "postgresql+asyncpg://ambient"


def snapshot() -> dict[str, Any]:
    return {
        "schema": "shizzle-library-snapshot-v1",
        "tracks": [
            {"id": str(TRACK_ID), "title": "Test Track", "generation": 1},
        ],
    }


class FakeRun:
    """Records every subprocess call's argv and env."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, command: list[str], env: dict[str, str] | None = None, **_: Any
    ) -> subprocess.CompletedProcess:
        self.calls.append({"argv": list(command), "env": dict(env or {})})
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")


class MigrateAndAuditCoordinatorTests(unittest.TestCase):
    def run_main(
        self, argv: list[str]
    ) -> tuple[int, FakeRun, list[dict[str, Any]], list[str], io.StringIO, io.StringIO]:
        runner = FakeRun()
        checkpoints: list[dict[str, Any]] = []
        parent_databases: list[str] = []

        async def fake_active_generation(database_url: str, _track_id: str) -> int:
            parent_databases.append(database_url)
            # Preflight sees the source generation; postflight the candidate.
            return 1 if len(parent_databases) == 1 else 2

        def fake_load_json(path: Path) -> dict[str, Any]:
            if "audit" in path.name:
                return {"failed_count": 0, "passed_count": 1}
            return snapshot()

        def fake_write_checkpoint(_path: Path, payload: dict[str, Any]) -> None:
            # Deep-copy: the coordinator mutates the same payload afterwards.
            checkpoints.append(json.loads(json.dumps(payload)))

        stdout, stderr = io.StringIO(), io.StringIO()
        full_argv = [
            "migrate_and_audit_delivery_library.py",
            "--snapshot",
            "in/snapshot.json",
            "--audit-report",
            "in/audit.json",
            "--evidence-dir",
            "ev",
            "--output",
            "out.json",
            "--migration-script",
            "migrate_delivery_generation.py",
            "--audit-script",
            "audit_delivery_library.py",
            "--track-id",
            str(TRACK_ID),
            *argv,
        ]
        with (
            mock.patch.object(subprocess, "run", runner),
            mock.patch.object(coordinator, "active_generation", fake_active_generation),
            mock.patch.object(coordinator, "load_json", fake_load_json),
            mock.patch.object(coordinator, "write_checkpoint", fake_write_checkpoint),
            mock.patch.object(sys, "argv", full_argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = coordinator.main()
        return code, runner, checkpoints, parent_databases, stdout, stderr

    def assert_children_got_explicit_database(self, runner: FakeRun) -> None:
        self.assertEqual(len(runner.calls), 3)  # publish, audit, activation
        argv_sets = [set(call["argv"]) for call in runner.calls]
        self.assertNotIn("--activate", argv_sets[0])
        self.assertNotIn("--database-url", argv_sets[0])
        self.assertNotIn("--database-url", argv_sets[1])
        self.assertIn("--activate", argv_sets[2])
        self.assertNotIn("--database-url", argv_sets[2])
        for call in runner.calls:
            self.assertEqual(call["env"].get("DATABASE_URL"), EXPLICIT)

    def test_explicit_url_overrides_divergent_ambient(self) -> None:
        with mock.patch.dict(os.environ, {"DATABASE_URL": AMBIENT}):
            code, runner, _, parent_databases, _, _ = self.run_main(
                ["--database-url", EXPLICIT]
            )
        self.assertEqual(code, 0)
        self.assert_children_got_explicit_database(runner)
        # Parent preflight/postflight also used the explicit database.
        self.assertEqual(parent_databases, [EXPLICIT, EXPLICIT])

    def test_explicit_url_without_ambient(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            code, runner, _, _, _, _ = self.run_main(["--database-url", EXPLICIT])
        self.assertEqual(code, 0)
        self.assert_children_got_explicit_database(runner)

    def test_dsn_credentials_never_leak(self) -> None:
        secret_url = "postgresql+asyncpg://user:secret@host/db"
        with mock.patch.dict(os.environ, {}, clear=True):
            code, runner, checkpoints, _, stdout, stderr = self.run_main(
                ["--database-url", secret_url]
            )
        self.assertEqual(code, 0)
        for call in runner.calls:
            self.assertEqual(call["env"].get("DATABASE_URL"), secret_url)
            self.assertNotIn("secret", " ".join(call["argv"]))
        for payload in checkpoints:
            self.assertNotIn("secret", json.dumps(payload))
        self.assertNotIn("secret", stdout.getvalue())
        self.assertNotIn("secret", stderr.getvalue())

    def test_missing_database_url_fails_before_any_child(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            code, runner, checkpoints, _, _, stderr = self.run_main([])
        self.assertEqual(code, 2)
        self.assertEqual(runner.calls, [])
        self.assertEqual(checkpoints, [])
        self.assertIn("DATABASE_URL or --database-url is required", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
