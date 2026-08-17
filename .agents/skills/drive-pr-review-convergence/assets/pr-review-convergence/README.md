# Self-Contained PR Review Convergence Package

This directory is the renderer's source template when it lives under the
skill's `assets/` tree. Do not run bootstrap there: first run
`scripts/render_pr_review_goal.py`. In the renderer's output directory this
same README accompanies the generated `tools/` directory and
`package-manifest.json`, and the commands below are runnable.

This rendered directory is the complete host-side package for driving one
public GitHub pull request through a bounded review-and-repair loop. It includes
the goal, acceptance contract, reviewer policy, E2B controller, E2B template
builder, project bootstrap hook, environment checks, and operating procedure.
No file in this package contains a secret.

## What is portable

Copy this entire rendered directory into the repository named in `goal.md`. It
is self-contained for public GitHub repositories whose build and tests can run
in a Linux E2B VM. Do not copy a package rendered for one pull request and use
it unchanged for another; render again so the repository, PR, checks, reviewers,
and validation commands are correct.

Private repositories, Docker-in-Docker, GPUs, Windows/macOS-only builds, local
hardware, private networks, and interactive browser tests require an explicit
adapter or a non-E2B validation lane. See `ENVIRONMENT.md` before claiming the
package supports one of those surfaces.

## Package contents

- `goal.md`, `facts.md`, and `plan.md`: the executable goal contract.
- `review-policy.json`: machine-readable reviewer and iteration policy.
- `setup.sh`: the only project-specific E2B bootstrap file.
- `tools/e2b_pr_sandbox.py`: one-writer sandbox controller and bundle handoff.
- `tools/e2b_pr_template.py`: versioned Linux E2B template builder.
- `bootstrap.ps1` and `bootstrap.sh`: non-secret host prerequisite checks.
- `ENVIRONMENT.md`: credentials, prerequisites, support matrix, and costs.
- `OPERATIONS.md`: copyable lifecycle commands and recovery procedure.
- `.gitignore.snippet`: controller state that must not be committed.
- `package-manifest.json`: SHA-256 inventory of every shipped source file.

## First use on Windows

From this package directory in PowerShell:

```powershell
.\bootstrap.ps1
uv run .\tools\e2b_pr_template.py --alias pr-review-v1 --tier standard
```

The renderer has already written the supplied `--setup-command` values into
`setup.sh`. Review that file for deterministic, credential-free behavior, then
run the `create` command in `OPERATIONS.md`. The E2B SDK is pinned inside both
Python scripts and is installed into uv's isolated cache on first use; no
project virtual environment is required.

## First use on Linux or WSL

```bash
./bootstrap.sh
uv run ./tools/e2b_pr_template.py --alias pr-review-v1 --tier standard
```

The controller maintains ignored host state beneath `.sandbox/e2b/`. Copy the
line in `.gitignore.snippet` into the target repository's `.gitignore` before a
run. Keep exactly one writer for a PR; add audit or test readers only for work
that can actually execute independently.

## Render a package for another PR

To render another PR, return to the standalone source project or installed
skill. From the source project:

```powershell
uv run tools/render_pr_review_goal.py `
  --pr-url https://github.com/OWNER/REPO/pull/123 `
  --repo OWNER/REPO --base-branch main `
  --required-check test --required-check lint `
  --reviewer greptile --reviewer coderabbit --reviewer cubic `
  --primary-reviewer greptile --minimum-primary-score 4 `
  --setup-command "uv sync --frozen" `
  --validation "uv run pytest" --validation "uv run ruff check ." `
  --output .pr-review/pr-123
```

The renderer refuses a non-empty output directory, validates the PR/repository
pair, rejects missing checks/reviewers/setup/validation, resolves every
placeholder, copies the runtime tools, and writes the integrity manifest.
