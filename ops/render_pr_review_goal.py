#!/usr/bin/env -S uv run --script
"""Render the repository-neutral PR convergence goal package."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "pr-review-convergence"
RUNTIME_FILES = {
    "e2b_pr_sandbox.py": ROOT / "ops" / "e2b_pr_sandbox.py",
    "e2b_pr_template.py": ROOT / "ops" / "e2b_pr_template.py",
}
TOKEN_RE = re.compile(r"\{\{[^{}]+\}\}")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def csv(values: list[str]) -> str:
    return ", ".join(values)


def validate(args: argparse.Namespace) -> None:
    if not REPO_RE.fullmatch(args.repo):
        raise SystemExit("--repo must be OWNER/REPO")
    parsed = urlparse(args.pr_url)
    expected_prefix = f"/{args.repo}/pull/"
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise SystemExit("--pr-url must be an https://github.com pull-request URL")
    if not parsed.path.startswith(expected_prefix):
        raise SystemExit("--pr-url does not match --repo")
    suffix = parsed.path.removeprefix(expected_prefix).strip("/")
    if not suffix.isdigit():
        raise SystemExit("--pr-url must end in a numeric pull-request number")
    if not args.required_check:
        raise SystemExit("provide at least one --required-check")
    if not args.reviewer:
        raise SystemExit("provide at least one --reviewer")
    if args.primary_reviewer not in args.reviewer:
        raise SystemExit("--primary-reviewer must also be supplied as --reviewer")
    if not 0 <= args.minimum_primary_score <= 5:
        raise SystemExit("--minimum-primary-score must be between 0 and 5")
    if not args.validation:
        raise SystemExit("provide at least one --validation")
    if not args.setup_command:
        raise SystemExit("provide at least one --setup-command")
    if args.max_iterations < 1:
        raise SystemExit("--max-iterations must be positive")
    if args.quiet_window_minutes < 1:
        raise SystemExit("--quiet-window-minutes must be positive")


def render(args: argparse.Namespace) -> list[Path]:
    validate(args)
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"refusing non-empty output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    replacements = {
        "{{PR_URL}}": args.pr_url,
        "{{PR_NUMBER}}": args.pr_url.rstrip("/").rsplit("/", 1)[-1],
        "{{OWNER/REPO}}": args.repo,
        "{{BASE_BRANCH}}": args.base_branch,
        "{{REQUIRED_CHECKS}}": csv(args.required_check),
        "{{CONFIGURED_REVIEWERS}}": csv(args.reviewer),
        "{{PRIMARY_REVIEWER}}": args.primary_reviewer,
        "{{PRIMARY_REVIEWER_MIN_SCORE}}": str(args.minimum_primary_score),
        "{{ADVISORY_REVIEWERS}}": csv(
            [reviewer for reviewer in args.reviewer if reviewer != args.primary_reviewer]
        )
        or "none",
        "{{ADVISORY_REVIEWERS_JSON}}": json.dumps(
            [reviewer for reviewer in args.reviewer if reviewer != args.primary_reviewer]
        ),
        "{{VALIDATION_COMMANDS}}": "; ".join(args.validation),
        "{{SETUP_COMMANDS}}": "\n".join(args.setup_command),
        "{{MAX_ITERATIONS}}": str(args.max_iterations),
        "{{QUIET_WINDOW_MINUTES}}": str(args.quiet_window_minutes),
    }
    written: list[Path] = []
    for source in sorted(TEMPLATE_DIR.iterdir()):
        destination = output / source.name
        if source.suffix in {".json", ".md", ".sh"}:
            text = source.read_text(encoding="utf-8")
            for token, value in replacements.items():
                text = text.replace(token, value)
            unknown = TOKEN_RE.findall(text)
            if unknown:
                raise SystemExit(
                    f"unrendered placeholders in {source.name}: {', '.join(unknown)}"
                )
            destination.write_text(text, encoding="utf-8", newline="\n")
        else:
            shutil.copy2(source, destination)
        written.append(destination)

    tools_dir = output / "tools"
    tools_dir.mkdir()
    for name, source in sorted(RUNTIME_FILES.items()):
        destination = tools_dir / name
        shutil.copy2(source, destination)
        written.append(destination)

    manifest_path = output / "package-manifest.json"
    manifest = {
        "schemaVersion": 1,
        "package": "pr-review-convergence",
        "containsSecrets": False,
        "files": [
            {
                "path": path.relative_to(output).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(written)
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    written.append(manifest_path)
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base-branch", required=True)
    parser.add_argument("--required-check", action="append", default=[])
    parser.add_argument("--reviewer", action="append", default=[])
    parser.add_argument("--primary-reviewer", default="greptile")
    parser.add_argument("--minimum-primary-score", type=int, default=4)
    parser.add_argument("--validation", action="append", default=[])
    parser.add_argument("--setup-command", action="append", default=[])
    parser.add_argument("--max-iterations", type=int, default=2)
    parser.add_argument("--quiet-window-minutes", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    for path in render(args):
        print(path)


if __name__ == "__main__":
    main()
