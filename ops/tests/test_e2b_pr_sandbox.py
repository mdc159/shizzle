from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "e2b_pr_sandbox.py"
SPEC = importlib.util.spec_from_file_location("e2b_pr_sandbox", MODULE_PATH)
assert SPEC and SPEC.loader
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class ControllerUnitTests(unittest.TestCase):
    def test_run_id_is_role_and_pr_specific(self) -> None:
        run_id = controller.new_run_id("audit-security", 4)
        self.assertRegex(run_id, r"^pr4-audit-security-\d{8}T\d{6}Z-[0-9a-f]{8}$")

    def test_role_validation_accepts_reader_lanes(self) -> None:
        self.assertEqual(controller.validate_role("writer"), "writer")
        self.assertEqual(controller.validate_role("audit-security"), "audit-security")
        self.assertEqual(controller.validate_role("test-workflows"), "test-workflows")
        with self.assertRaises(controller.ControllerError):
            controller.validate_role("writer-two")

    def test_atomic_record_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runs = Path(temp) / "runs"
            with patch.object(controller, "RUNS_DIR", runs):
                record = {"run_id": "pr4-test", "state": "planned"}
                controller.save_record(record)
                loaded = controller.load_record("pr4-test")
                self.assertEqual(loaded["state"], "planned")
                self.assertIn("updated_at", loaded)
                self.assertEqual(list(runs.glob("*.json")), [runs / "pr4-test.json"])

    def test_one_active_writer_per_pr(self) -> None:
        existing = {
            "run_id": "pr4-writer-existing",
            "repo": "mdc159/shizzle",
            "pr_number": 4,
            "role": "writer",
            "state": "paused",
        }
        with patch.object(controller, "list_records", return_value=[existing]):
            with self.assertRaisesRegex(controller.ControllerError, "active writer"):
                controller.ensure_writer_available("mdc159/shizzle", 4, "writer")
            controller.ensure_writer_available("mdc159/shizzle", 4, "audit")

        existing["state"] = "paused-failed"
        with (
            patch.object(controller, "list_records", return_value=[existing]),
            self.assertRaisesRegex(controller.ControllerError, "active writer"),
        ):
            controller.ensure_writer_available("mdc159/shizzle", 4, "writer")

    def test_initial_record_has_pinned_head_and_pause_lifecycle(self) -> None:
        pull = {
            "url": "https://github.com/mdc159/shizzle/pull/4",
            "repo": "mdc159/shizzle",
            "pr_number": 4,
            "base_ref": "main",
            "base_sha": "a" * 40,
            "head_repo": "mdc159/shizzle",
            "head_ref": "feature",
            "head_sha": "b" * 40,
        }
        record = controller.initial_record(pull, "writer", "template-v1", 600, "run-1")
        self.assertEqual(record["head_sha"], "b" * 40)
        self.assertEqual(
            record["lifecycle"], {"on_timeout": "pause", "auto_resume": True}
        )
        self.assertIsNone(record["sandbox_id"])
        json.dumps(record)

    def test_custom_setup_file_is_accepted_by_create_and_fanout(self) -> None:
        parser = controller.build_parser()
        create = parser.parse_args(
            [
                "create",
                "--repo",
                "owner/repo",
                "--pr",
                "7",
                "--setup-file",
                "setup.sh",
            ]
        )
        self.assertEqual(create.setup_file, "setup.sh")
        fanout = parser.parse_args(
            [
                "fanout",
                "--repo",
                "owner/repo",
                "--pr",
                "7",
                "--roles",
                "audit,test",
                "--setup-file",
                "setup.sh",
            ]
        )
        self.assertEqual(fanout.setup_file, "setup.sh")

    def test_sync_diff_accepts_incremental_base_ref(self) -> None:
        args = controller.build_parser().parse_args(
            [
                "sync-diff",
                "pr7-writer",
                ".sandbox/e2b/staging/pr7",
                "--base-ref",
                "refs/sandbox/e2b/pr7-writer",
                "--replace",
            ]
        )
        self.assertEqual(args.base_ref, "refs/sandbox/e2b/pr7-writer")
        self.assertTrue(args.replace)

    def test_push_parser_defaults_to_origin(self) -> None:
        args = controller.build_parser().parse_args(["push", "pr7-writer"])
        self.assertEqual(args.remote, "origin")

    def test_push_passes_one_argument_safe_refspec(self) -> None:
        expected = "a" * 40
        harvested = "b" * 40
        record = {
            "run_id": "pr7-writer",
            "role": "writer",
            "repo": "owner/repo",
            "pr_number": 7,
            "artifacts": [
                {
                    "imported_ref": "refs/sandbox/e2b/pr7-writer",
                    "expected_remote_head": expected,
                    "sha": harvested,
                }
            ],
        }
        before = {
            "state": "open",
            "repo": "owner/repo",
            "head_repo": "owner/repo",
            "head_ref": "feature",
            "head_sha": expected,
        }
        after = {**before, "head_sha": harvested}
        with (
            patch.object(controller, "load_record", return_value=record),
            patch.object(
                controller, "resolve_pull_request", side_effect=[before, after]
            ),
            patch.object(controller, "host_command") as command,
            patch.object(controller, "save_record"),
        ):
            controller.command_push(Namespace(run_id="pr7-writer", remote="origin"))
        command.assert_any_call(
            [
                "git",
                "push",
                "origin",
                "refs/sandbox/e2b/pr7-writer:refs/heads/feature",
            ]
        )

    def test_push_retries_pr_head_read_without_repeating_push(self) -> None:
        expected = "a" * 40
        harvested = "b" * 40
        record = {
            "run_id": "pr7-writer",
            "role": "writer",
            "repo": "owner/repo",
            "pr_number": 7,
            "artifacts": [
                {
                    "imported_ref": "refs/sandbox/e2b/pr7-writer",
                    "expected_remote_head": expected,
                    "sha": harvested,
                }
            ],
        }
        before = {
            "state": "open",
            "repo": "owner/repo",
            "head_repo": "owner/repo",
            "head_ref": "feature",
            "head_sha": expected,
        }
        after = {**before, "head_sha": harvested}
        with (
            patch.object(controller, "load_record", return_value=record),
            patch.object(
                controller,
                "resolve_pull_request",
                side_effect=[before, before, after],
            ),
            patch.object(controller, "host_command") as command,
            patch.object(controller.time, "sleep") as sleep,
            patch.object(controller, "save_record"),
        ):
            controller.command_push(Namespace(run_id="pr7-writer", remote="origin"))
        push_calls = [
            call
            for call in command.call_args_list
            if call.args[0][:2] == ["git", "push"]
        ]
        self.assertEqual(len(push_calls), 1)
        sleep.assert_called_once_with(1)
        self.assertEqual(record["artifacts"][-1]["post_push_attempts"], 2)

    def test_push_reconciles_already_visible_artifact_without_push(self) -> None:
        harvested = "b" * 40
        record = {
            "run_id": "pr7-writer",
            "role": "writer",
            "repo": "owner/repo",
            "pr_number": 7,
            "artifacts": [
                {
                    "imported_ref": "refs/sandbox/e2b/pr7-writer",
                    "expected_remote_head": "a" * 40,
                    "sha": harvested,
                }
            ],
        }
        current = {
            "state": "open",
            "repo": "owner/repo",
            "head_repo": "owner/repo",
            "head_ref": "feature",
            "head_sha": harvested,
        }
        with (
            patch.object(controller, "load_record", return_value=record),
            patch.object(controller, "resolve_pull_request", return_value=current),
            patch.object(controller, "host_command") as command,
            patch.object(controller, "save_record"),
        ):
            controller.command_push(Namespace(run_id="pr7-writer", remote="origin"))
        self.assertFalse(
            any(call.args[0][:2] == ["git", "push"] for call in command.call_args_list)
        )
        self.assertIn("reconciled_at", record["artifacts"][-1])


if __name__ == "__main__":
    unittest.main()
