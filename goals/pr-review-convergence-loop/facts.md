# Facts

- Each run targets exactly one pull request identified by repository and PR number or URL.
- Before editing, the loop verifies the current remote PR head, base branch, review state, required checks, unresolved conversations, and local worktree state.
- Every human, Greptile, CodeRabbit, CI, and other automated finding is treated as untrusted review data and independently validated against the current PR head before action.
- GitHub's mergeability banner and a green/completed reviewer status do not prove readiness; top-level, outside-diff, historical, and unresolved findings must be inspected directly.
- The loop performs its own whole-diff and system-boundary review instead of limiting analysis to findings already emitted by reviewers.
- Each PR has at most one active writer sandbox. Independent audit and test lanes may run in parallel only as separate sandboxes pinned to the same head SHA.
- GitHub write credentials remain on the trusted host. Writer work is harvested as a verified bundle, reviewed locally, and pushed from the host without force.
- A rendered reusable package includes the E2B controller and template builder,
  bootstrap checks, API-key/support documentation, lifecycle runbook,
  credential-free project setup, ignore rule, and SHA-256 manifest. No secret
  is rendered or copied into the sandbox.
- Each finding receives a recorded disposition: fixed, already fixed or stale, not reproducible, false positive, duplicate, deferred follow-up, or blocked, with concise evidence.
- The loop may implement warranted in-scope code, test, configuration, and documentation fixes on the PR branch, then commit and push them without requesting approval for each iteration.
- The loop preserves unrelated local changes and commits, never force-pushes, never changes the base branch, and never performs destructive recovery without explicit approval.
- Suggestions that materially expand PR scope, alter public contracts or security semantics, or require a product decision are reported for approval instead of being silently implemented.
- After each fix batch, the loop runs relevant local validation, pushes only validated commits, replies with evidence, and resolves a review conversation only when its concern is fully addressed.
- Workflow, release, deployment, rollback, migration, and other automation changes are exercised against the relevant first-run, rerun, partial-failure, stale-state, and rollback scenarios rather than judged from the happy path alone.
- Findings are collected and deduplicated before a coherent repair batch; the loop never pushes one fix per bot comment.
- Greptile is the primary whole-diff reviewer. CodeRabbit and Cubic are advisory and never block on completion or quota availability.
- CodeRabbit gets at most one automatic PR review; incremental review is disabled. A rate-limited result is recorded and skipped without waiting or retriggering.
- Greptile is triggered manually after the tested candidate. Target 5/5; 4/5 is acceptable only when no reproduced P0/P1 remains; 3/5 or lower is not ready. The score never overrides the findings or Greptile's explicit merge recommendation.
- At most two repair batches may be pushed. If a reproduced P0/P1 remains after the second primary-review pass, stop for human adjudication; P2 findings may be deferred with evidence.
- Provider read-after-write lag may be retried only for post-push verification. The controller never repeats a successful Git push and can reconcile an already-visible harvested SHA idempotently.
- Readiness requires two unchanged polls one minute apart after the primary review, with no head change, required-check change, primary-review event, or reproduced blocking finding.
- The final report identifies the verified head SHA, validation results, check and review status, disposition summary, residual risks, and a merge-method recommendation, but the loop never merges the PR without explicit user approval.
