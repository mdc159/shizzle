from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "e2b_pr_sandbox.py"
SPEC = importlib.util.spec_from_file_location("e2b_pr_sandbox", MODULE_PATH)
assert SPEC and SPEC.loader
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)

SKILL_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "drive-pr-review-convergence"
    / "scripts"
    / "e2b_pr_sandbox.py"
)
SKILL_SPEC = importlib.util.spec_from_file_location(
    "skill_e2b_pr_sandbox", SKILL_MODULE_PATH
)
assert SKILL_SPEC and SKILL_SPEC.loader
skill_controller = importlib.util.module_from_spec(SKILL_SPEC)
SKILL_SPEC.loader.exec_module(skill_controller)


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
        self.assertEqual(record["controller_sdk"], "e2b==2.35.0")
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

    def test_sync_diff_requires_patch_base_to_ancestor_source(self) -> None:
        patch_base = "a" * 40
        source_head = "b" * 40
        with tempfile.TemporaryDirectory() as temp:
            state_root = Path(temp) / "state"
            source = state_root / "staging" / "pr7"
            (source / ".git").mkdir(parents=True)
            record = {
                "run_id": "pr7-writer",
                "head_sha": "0" * 40,
                "checkout_path": "/workspace/repo",
                "state": "paused",
            }

            def command(args, **_kwargs):
                if args[:3] == ["git", "rev-parse", "HEAD"]:
                    return SimpleNamespace(stdout=source_head + "\n")
                if args[:3] == ["git", "rev-parse", "--verify"]:
                    return SimpleNamespace(stdout=patch_base + "\n")
                if args[:4] == ["git", "diff", "--binary", "--no-ext-diff"]:
                    return SimpleNamespace(stdout="diff --git a/x b/x\n")
                return SimpleNamespace(stdout="")

            sandbox = MagicMock()
            with (
                patch.object(controller, "STATE_ROOT", state_root),
                patch.object(controller, "load_record", return_value=record),
                patch.object(controller, "host_command", side_effect=command) as host,
                patch.object(controller, "connect", return_value=sandbox),
                patch.object(controller, "remote_run", return_value=SimpleNamespace(stdout="")),
                patch.object(controller, "save_record"),
            ):
                controller.command_sync_diff(
                    Namespace(
                        run_id="pr7-writer",
                        source_worktree=str(source),
                        replace=True,
                        base_ref="refs/sandbox/e2b/pr7-writer",
                        timeout=60,
                    )
                )
            host.assert_any_call(
                ["git", "merge-base", "--is-ancestor", patch_base, source_head],
                cwd=source,
            )

    def test_push_parser_defaults_to_origin(self) -> None:
        args = controller.build_parser().parse_args(["push", "pr7-writer"])
        self.assertEqual(args.remote, "origin")

    def test_harvest_guard_requires_commit_beyond_recorded_pr_head(self) -> None:
        head = "a" * 40
        record = {
            "run_id": "pr7-writer",
            "role": "writer",
            "checkout_path": "/workspace/repo",
            "base_sha": "0" * 40,
            "head_sha": head,
        }
        with (
            patch.object(controller, "load_record", return_value=record),
            patch.object(controller, "connect", return_value=MagicMock()),
            patch.object(controller, "remote_run", side_effect=RuntimeError("stop")) as run,
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            controller.command_harvest(
                Namespace(run_id="pr7-writer", timeout=60, no_import=False)
            )
        script = run.call_args.args[1]
        self.assertIn(f"!= {head}", script)
        self.assertIn(f"merge-base --is-ancestor {head} HEAD", script)

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
            command.return_value.stdout = harvested
            controller.command_push(Namespace(run_id="pr7-writer", remote="origin"))
        command.assert_any_call(
            [
                "git",
                "push",
                "origin",
                f"{harvested}:refs/heads/feature",
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
            command.return_value.stdout = harvested
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

    def test_push_rejects_moved_imported_ref(self) -> None:
        expected = "a" * 40
        harvested = "b" * 40
        record = {
            "run_id": "pr7-writer",
            "role": "writer",
            "repo": "owner/repo",
            "pr_number": 7,
            "artifacts": [{
                "imported_ref": "refs/sandbox/e2b/pr7-writer",
                "expected_remote_head": expected,
                "sha": harvested,
            }],
        }
        current = {
            "state": "open",
            "repo": "owner/repo",
            "head_repo": "owner/repo",
            "head_ref": "feature",
            "head_sha": expected,
        }
        with (
            patch.object(controller, "load_record", return_value=record),
            patch.object(controller, "resolve_pull_request", return_value=current),
            patch.object(
                controller,
                "host_command",
                return_value=SimpleNamespace(stdout="c" * 40 + "\n"),
            ) as command,
            self.assertRaisesRegex(controller.ControllerError, "imported ref moved"),
        ):
            controller.command_push(Namespace(run_id="pr7-writer", remote="origin"))
        self.assertFalse(
            any(call.args[0][:2] == ["git", "push"] for call in command.call_args_list)
        )

    def test_private_bundle_rejects_multiple_advertised_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup = root / "setup.sh"
            setup.write_text("#!/bin/sh\n", encoding="utf-8")
            bundle = root / "source.bundle"
            bundle.write_bytes(b"not uploaded")
            source_repo = root / "repo"
            (source_repo / ".git").mkdir(parents=True)
            pull = {
                "url": "https://github.com/owner/repo/pull/7",
                "repo": "owner/repo",
                "pr_number": 7,
                "base_ref": "main",
                "base_sha": "a" * 40,
                "head_repo": "owner/repo",
                "head_ref": "feature",
                "head_sha": "b" * 40,
                "state": "open",
            }
            heads = (
                f"{'b' * 40} refs/heads/feature\n"
                f"{'c' * 40} refs/heads/private\n"
            )
            with (
                patch.object(skill_controller, "resolve_pull_request", return_value=pull),
                patch.object(skill_controller, "ensure_writer_available"),
                patch.object(
                    skill_controller,
                    "host_command",
                    return_value=SimpleNamespace(stdout=heads),
                ),
                self.assertRaisesRegex(
                    skill_controller.ControllerError, "exactly one head ref"
                ),
            ):
                skill_controller.provision(
                    "owner/repo", 7, "writer", "template", 60,
                    str(setup), False, str(bundle), str(source_repo)
                )

    def test_private_bundle_header_rejects_prerequisites(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "source.bundle"
            bundle.write_bytes(
                b"# v2 git bundle\n"
                + b"-"
                + b"a" * 40
                + b" prerequisite\n"
                + b"b" * 40
                + b" refs/heads/feature\n\n"
            )
            self.assertTrue(skill_controller.bundle_has_prerequisites(bundle))

            bundle.write_bytes(
                b"# v2 git bundle\n"
                + b"b" * 40
                + b" refs/heads/feature\n\n"
            )
            self.assertFalse(skill_controller.bundle_has_prerequisites(bundle))

    def test_private_bundle_rejects_non_branch_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup = root / "setup.sh"
            setup.write_text("#!/bin/sh\n", encoding="utf-8")
            bundle = root / "source.bundle"
            bundle.write_bytes(b"not uploaded")
            source_repo = root / "repo"
            (source_repo / ".git").mkdir(parents=True)
            pull = {
                "url": "https://github.com/owner/repo/pull/7",
                "repo": "owner/repo",
                "pr_number": 7,
                "base_ref": "main",
                "base_sha": "a" * 40,
                "head_repo": "owner/repo",
                "head_ref": "feature",
                "head_sha": "b" * 40,
                "state": "open",
            }
            with (
                patch.object(skill_controller, "resolve_pull_request", return_value=pull),
                patch.object(skill_controller, "ensure_writer_available"),
                patch.object(
                    skill_controller,
                    "host_command",
                    return_value=SimpleNamespace(
                        stdout=f"{'b' * 40} refs/tags/feature\n"
                    ),
                ),
                self.assertRaisesRegex(
                    skill_controller.ControllerError, "exactly match"
                ),
            ):
                skill_controller.provision(
                    "owner/repo", 7, "writer", "template", 60,
                    str(setup), False, str(bundle), str(source_repo)
                )


if __name__ == "__main__":
    unittest.main()
