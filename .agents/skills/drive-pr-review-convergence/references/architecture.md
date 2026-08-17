# Architecture

## Components

1. `tools/render_pr_review_goal.py` validates PR-specific inputs and emits a
   closed package with no unresolved placeholders.
2. `tools/e2b_pr_template.py` builds a versioned Linux E2B template with Node
   22, Git, uv, jq, rsync, shellcheck, curl, and basic transport tools.
3. `tools/e2b_pr_sandbox.py` owns sandbox lifecycle, exact-head checkout,
   candidate replay, execution, pause/resume, bundle harvest, and guarded push.
4. `templates/pr-review-convergence/` defines the human and machine acceptance
   contract rendered for one PR.
5. The installable skill tells an agent how to use those deterministic assets
   without loading the full documentation into every context window.

## Identity and state

Every observation is tied to one remote PR head SHA. Controller state is stored
atomically under `<package>/.sandbox/e2b/runs/`. Each record includes repository,
PR, role, base/head identity, template, sandbox ID, lifecycle, setup digest,
artifacts, and last error.

The controller uses these role classes:

- `writer`: the only lane allowed to produce an importable bundle or push.
- `audit-*`: read-only analysis such as security, workflow, or architecture.
- `test-*`: independent validation partitions.

An active or paused writer record prevents creation of a second writer for the
same PR. Multiple readers may exist but must consume the same authoritative
candidate.

## Candidate flow

```text
remote head SHA
  -> credential-free E2B clone
  -> exact detached source identity
  -> one host staging worktree beneath .sandbox/e2b/staging
  -> binary-safe candidate diff
  -> writer and optional readers
  -> validated writer commit
  -> git bundle
  -> host bundle verification and imported ref
  -> remote-head compare-and-swap check
  -> one non-force push
  -> bounded read-after-write verification
```

The staging worktree is the candidate authority. Reader outputs are evidence;
they never become independent source branches. This prevents parallel lanes
from silently producing incompatible fixes.

## Lifecycle

Sandboxes are created with `on_timeout=pause` and `auto_resume=true`. The
controller records intent before creating the remote resource. A failed
provision attempts to pause and preserves a `paused-failed` record for
diagnosis. `destroy` is permanent and requires the exact run ID as confirmation.

## Readiness state machine

```text
INTAKE
  -> TRIAGED
  -> CANDIDATE_VALIDATED
  -> PUSHED
  -> REQUIRED_CI_GREEN
  -> PRIMARY_REVIEW_COMPLETE
  -> QUIET_WINDOW
  -> READY_TO_MERGE (stop)

Any head drift -> INTAKE
Any reproduced P0/P1 -> TRIAGED, if repair budget remains
Repair budget exhausted with blocker -> BLOCKED
Product/security/scope decision needed -> HUMAN_DECISION
```

The state machine has no automatic merge transition.

## Why one writer

Multiple sandboxes reduce wall time only when work is independent. Multiple
writers introduce merge conflicts, divergent candidate identities, duplicate
review triggers, and unclear ownership. One writer plus targeted readers gives
parallel evidence without parallel truth.
