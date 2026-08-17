from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "render_pr_review_goal.py"
SPEC = importlib.util.spec_from_file_location("render_pr_review_goal", MODULE_PATH)
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


class RenderGoalTests(unittest.TestCase):
    def args(self, output: Path):
        return renderer.build_parser().parse_args(
            [
                "--pr-url",
                "https://github.com/example/project/pull/12",
                "--repo",
                "example/project",
                "--base-branch",
                "main",
                "--required-check",
                "test",
                "--reviewer",
                "greptile",
                "--reviewer",
                "coderabbit",
                "--validation",
                "uv run pytest",
                "--setup-command",
                "uv sync --frozen",
                "--output",
                str(output),
            ]
        )

    def test_renders_complete_package_without_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "goal"
            written = renderer.render(self.args(output))
            self.assertEqual(
                {path.relative_to(output).as_posix() for path in written},
                {
                    ".gitignore.snippet",
                    "ENVIRONMENT.md",
                    "OPERATIONS.md",
                    "README.md",
                    "bootstrap.ps1",
                    "bootstrap.sh",
                    "facts.md",
                    "goal.md",
                    "package-manifest.json",
                    "plan.md",
                    "review-policy.json",
                    "setup.sh",
                    "tools/e2b_pr_sandbox.py",
                    "tools/e2b_pr_template.py",
                },
            )
            rendered = "\n".join(
                path.read_text(encoding="utf-8")
                for path in written
                if path.suffix in {".json", ".md", ".ps1", ".py", ".sh"}
            )
            self.assertNotRegex(rendered, renderer.TOKEN_RE)
            self.assertIn("example/project", rendered)
            self.assertIn("uv run pytest", rendered)
            self.assertIn(
                "uv sync --frozen", (output / "setup.sh").read_text(encoding="utf-8")
            )
            policy = json.loads(
                (output / "review-policy.json").read_text(encoding="utf-8")
            )
            self.assertEqual(policy["primaryReviewer"]["name"], "greptile")
            self.assertTrue(policy["primaryReviewer"]["blockExplicitDoNotMerge"])
            self.assertTrue(policy["findings"]["requireEvidenceBackedDispositions"])
            self.assertEqual(policy["advisoryReviewers"], ["coderabbit"])
            self.assertEqual(policy["repair"]["maxBatches"], 2)
            manifest = json.loads(
                (output / "package-manifest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(manifest["containsSecrets"])
            entries = {item["path"]: item["sha256"] for item in manifest["files"]}
            self.assertEqual(
                set(entries),
                {
                    path.relative_to(output).as_posix()
                    for path in written
                    if path.name != "package-manifest.json"
                },
            )
            self.assertEqual(
                entries["tools/e2b_pr_sandbox.py"],
                hashlib.sha256(renderer.RUNTIME_FILES["e2b_pr_sandbox.py"].read_bytes()).hexdigest(),
            )
            self.assertEqual(
                entries["goal.md"],
                hashlib.sha256((output / "goal.md").read_bytes()).hexdigest(),
            )
            self.assertTrue((output / "bootstrap.sh").stat().st_mode & stat.S_IXUSR)
            self.assertTrue((output / "setup.sh").stat().st_mode & stat.S_IXUSR)
            self.assertIn(
                "E2B_API_KEY", (output / "ENVIRONMENT.md").read_text(encoding="utf-8")
            )
            self.assertNotIn("E2B_API_KEY=", rendered)

    def test_refuses_nonempty_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "goal"
            output.mkdir()
            (output / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(SystemExit):
                renderer.render(self.args(output))

    def test_rejects_mismatched_pr_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.args(Path(temporary) / "goal")
            args.pr_url = "https://github.com/other/project/pull/12"
            with self.assertRaises(SystemExit):
                renderer.render(args)

    def test_requires_project_setup_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.args(Path(temporary) / "goal")
            args.setup_command = []
            with self.assertRaises(SystemExit):
                renderer.render(args)

    def test_rejects_query_fragment_blank_and_secret_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.args(Path(temporary) / "goal-query")
            args.pr_url += "?diff=split"
            with self.assertRaises(SystemExit):
                renderer.render(args)

            args = self.args(Path(temporary) / "goal-blank")
            args.validation = ["   "]
            with self.assertRaises(SystemExit):
                renderer.render(args)

            args = self.args(Path(temporary) / "goal-secret")
            args.setup_command = ["API_TOKEN=literal uv sync"]
            with self.assertRaises(SystemExit):
                renderer.render(args)

    def test_primary_reviewer_is_json_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "goal"
            args = self.args(output)
            args.reviewer = ['grep"tile', "coderabbit"]
            args.primary_reviewer = 'grep"tile'
            renderer.render(args)
            policy = json.loads(
                (output / "review-policy.json").read_text(encoding="utf-8")
            )
            self.assertEqual(policy["primaryReviewer"]["name"], 'grep"tile')


if __name__ == "__main__":
    unittest.main()
