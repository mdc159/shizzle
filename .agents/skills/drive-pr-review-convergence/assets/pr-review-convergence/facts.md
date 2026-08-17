# Acceptance Facts

- One run targets exactly one pull request and one remote head SHA at a time.
- GitHub mergeability and green bot status are evidence inputs, not readiness.
- Enumerate inline threads, review bodies, issue comments, requested changes,
  CI checks, and configured reviewer state; do not rely on conversation comments
  alone.
- Normalize findings by stable provider identifier and assign exactly one of:
  `fixed`, `stale/already-fixed`, `not-reproducible`, `false-positive`,
  `duplicate`, `deferred-follow-up`, or `blocked`.
- Independently review the whole diff and affected boundaries; bots do not set
  the completeness boundary.
- There is at most one writer. Readers receive the same authoritative candidate
  and cannot harvest or push.
- The sandbox receives no GitHub write token. The host verifies and pushes the
  harvested writer bundle without force.
- `E2B_API_KEY` exists only in the trusted host process. The rendered package,
  project setup, sandbox environment, logs, and Git history contain no secret.
- The portable built-in clone path supports public GitHub repositories. Private
  repositories and unsupported Linux/runtime surfaces require a disclosed,
  separately approved adapter or validation lane.
- Relevant validation includes failure, retry, stale-state, partial-update,
  migration, and rollback behavior whenever those boundaries changed.
- A thread is resolved only after its concern is fixed or disproved with
  concise evidence.
- Findings are repaired in coherent batches, not one comment per commit.
- The primary reviewer is the only automated reviewer whose final-candidate
  completion gates readiness. Advisory reviewers are opportunistic inputs.
- A rate-limited or unavailable advisory reviewer is recorded and skipped; the
  loop never waits for its quota to refill.
- When Greptile is primary, 5/5 is the target. A 4/5 may pass only with no
  reproduced P0/P1; 3/5 or lower cannot pass. The score is never used without
  inspecting the findings and explicit merge recommendation.
- At most `{{MAX_ITERATIONS}}` repair batches may be pushed. Remaining P0/P1
  findings then produce a blocked human-adjudication outcome; P2 findings may
  be dispositioned to a follow-up ledger.
- Only a new commit, required-check change, primary-review event, or reproduced
  blocking finding resets the quiet window.
- The loop never merges without explicit user approval.
