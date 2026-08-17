# E2B PR Review Sandbox

Shizzle uses one reusable E2B template to create many disposable pull-request
sandboxes. The concurrency rule is **one writer per PR, zero or more readers**:

- The writer lane may edit and commit. It returns work as a verified git bundle.
- Audit and test lanes run independently at the same pinned PR head SHA.
- Sandboxes do not receive a GitHub write token. The trusted host verifies and
  imports a bundle, reviews it, and performs any later push explicitly.
- A run record is written before E2B provisioning and retained after teardown.

This avoids shared worktree, port, process, and Git state collisions while still
allowing security, workflow, and test analysis to run in parallel. Multiple
writer lanes are rejected by the controller.

## Build and check the template

```powershell
uv run ops/e2b_pr_template.py --alias pr-review-v1 --tier standard
uv run ops/e2b_pr_template.py --alias pr-review-audit-v1 --tier small
uv run ops/e2b_pr_sandbox.py doctor
```

The standard writer tier is four vCPUs and 8 GiB RAM. The small audit tier is
two vCPUs and 4 GiB RAM. Use the heavy tier only for demonstrated resource
needs. An alias identifies the software image and resource tier; each `create`
call still produces a separate sandbox instance.

## Create one lane or a fanout

```powershell
uv run ops/e2b_pr_sandbox.py create --repo mdc159/shizzle --pr 4 --role writer
uv run ops/e2b_pr_sandbox.py fanout --repo mdc159/shizzle --pr 4 `
  --roles audit-security,audit-workflows,test-regression `
  --template shizzle-pr-audit-v1
```

For another repository, pass its versioned bootstrap script explicitly:

```powershell
uv run ops/e2b_pr_sandbox.py create --repo OWNER/REPO --pr 123 `
  --role writer --template pr-review-v1 `
  --setup-file templates/pr-review-convergence/setup.sh
```

The setup script receives the sandbox checkout path as its only argument. Its
SHA-256 is recorded in the run manifest so environment drift is visible.

Provisioning resolves the pull request through the host GitHub CLI, clones the
public repository, fetches `refs/pull/<number>/head`, checks out the exact
recorded SHA, installs Shizzle dependencies, and pauses the sandbox by default.
The state lives beneath `.sandbox/e2b/`, which is ignored by Git.

Useful lifecycle commands:

```powershell
uv run ops/e2b_pr_sandbox.py list
uv run ops/e2b_pr_sandbox.py resume RUN_ID
uv run ops/e2b_pr_sandbox.py exec RUN_ID -- git status --short
uv run ops/e2b_pr_sandbox.py apply-patch RUN_ID .sandbox/e2b/staging/fix.patch
uv run ops/e2b_pr_sandbox.py sync-diff RUN_ID .sandbox/e2b/staging/pr-worktree
uv run ops/e2b_pr_sandbox.py sync-diff RUN_ID .sandbox/e2b/staging/pr-worktree `
  --base-ref refs/sandbox/e2b/RUN_ID --replace
uv run ops/e2b_pr_sandbox.py pause RUN_ID
```

`apply-patch` uploads a host-authored candidate, runs `git apply --check`,
applies it inside E2B, and rejects whitespace errors before returning. The same
candidate can be replayed into audit/test lanes for independent pre-push
validation. Only the canonical writer may harvest a bundle, so reader lanes
cannot become competing authorities.

For larger changes, `sync-diff` accepts only an ignored Git worktree beneath
`.sandbox/e2b/staging/`, verifies that its starting HEAD equals the run's
recorded PR SHA, resets that disposable lane to the recorded head, checks
whitespace, includes intent-to-add files, and applies the resulting binary-safe
diff through the same remote patch gate. This makes candidate replay
deterministic across repeated iterations.

After harvesting and pushing a writer commit, use `--base-ref` to generate only
the next iteration's delta and require the target lane to be at that imported
ref. Incremental replay does not reset the lane by default. Clean readers still
pinned to the original head can continue using the full replay form without
`--base-ref`. During pre-commit iteration, add `--replace` to discard only that
disposable lane's prior candidate and replay the revised authoritative delta
from the same verified base.

E2B auto-resumes a paused sandbox when the controller reconnects. Pause between
review polls so preserved state does not consume continuous compute.

## Harvest writer work

Commit all intended changes inside the writer sandbox, then run:

```powershell
uv run ops/e2b_pr_sandbox.py harvest RUN_ID
```

Harvest refuses dirty worktrees and runs without commits. It downloads a Git
bundle, verifies it locally, and imports its head at
`refs/sandbox/e2b/<run-id>`. It does not alter the current branch, index, or
worktree. Inspect the ref and tests, then use the controller's argument-safe
host push:

```powershell
uv run ops/e2b_pr_sandbox.py push RUN_ID
```

`push` re-reads the PR, requires its head to equal the head recorded during
harvest, rejects fork heads, passes one non-force refspec directly to Git, and
verifies the resulting PR head. Its bounded retries cover provider
read-after-write lag and never repeat the push. Do not hand-assemble a
PowerShell refspec.

Permanent destruction is intentionally separate and confirmation-gated:

```powershell
uv run ops/e2b_pr_sandbox.py destroy RUN_ID --confirm RUN_ID
```

Do not destroy a sandbox until artifacts are harvested and the user has
approved deletion. Pausing is the normal handoff state.

## What this design does not cover

The Linux CPU template covers Git, Python/uv, Node/npm, shell tooling, and most
repository tests. It is not a universal execution boundary. Use a disclosed
fallback for Windows-only behavior, privileged Docker, GPU workloads, hardware,
private networks, production credentials, or browser/desktop integration.
Hosted CI and external deployment results remain independent evidence.

## Design provenance

The lifecycle and SDK behavior follow the current E2B documentation. The
one-sandbox-per-agent pattern was informed by Disler's `agent-sandbox-skill`.
The durable pre-provision record, credential boundary, gated teardown, verified
bundle harvest, and one-sandbox-per-fanout-arm patterns were informed by the
Inkwell software-factory example. This implementation is original and tailored
to Shizzle rather than copied from either project.
