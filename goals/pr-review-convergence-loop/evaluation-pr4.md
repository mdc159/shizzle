# PR #4 Workflow Evaluation

Evaluated against `mdc159/shizzle` PR #4 from starting head `c91ad6d` through
six repair cycles and coordinator-policy head `9db8405`.

## Performance

- Writer provisioning: 27.64 seconds.
- Project-neutral small-template provisioning with a custom setup file: 23.3
  seconds. The smoke run verified exact-head checkout, a clean worktree, project
  importability, setup SHA-256 recording, auto-resume, and pause.
- A second-repository portability smoke provisioned Inkwell PR #10 from a fork
  in 13.4 seconds with `pr-review-audit-v1`, checked out exact head `23e765b`,
  recorded setup digest `77f99e1d...`, passed clean-worktree and diff checks,
  and paused. This exercised generic repository/fork metadata without granting
  GitHub credentials or write authority.
- Candidate synchronization into an existing lane: approximately 2.0-3.3
  seconds.
- Final exact-candidate validation with three lanes: 45.3 seconds wall time.
  The component durations were 45.2 seconds (library), 14.8 seconds
  (stemsplit/player), and 3.0 seconds (automation), or about 63 seconds if run
  serially. Parallelism saved approximately 28% on that pass; an earlier pass
  saved approximately 34%.
- A later three-lane pass took 43.0 seconds wall time versus 71.3 seconds of
  summed lane time, a 40% reduction.
- Hosted reviewer latency exceeded local validation latency, so extra lanes
  improved repair-cycle speed but did not dominate end-to-end convergence.
- The loop dispositioned 46 review threads across six repair
  cycles before the final quiet-window gate. This is the clearest evidence that
  a green check badge is only an input: Cubic posted five new valid findings
  after the preceding head had already passed required CI.

## What the live trial changed

- Reconfigured Windows stdout/stderr as UTF-8 after a Unicode checkmark caused
  a CP1252 controller crash.
- Preserved remote stdout/stderr when E2B commands fail so diagnostics are not
  hidden behind a provider exception.
- Added candidate replay to reader lanes, then made repeated replay reset only
  the validated disposable checkout to its recorded head before applying the
  authoritative diff.
- Added a project-supplied `--setup-file` contract and recorded its SHA-256,
  removing the controller's dependence on a Shizzle-only bootstrap.
- Added incremental and replace-mode candidate replay after live review cycles
  showed that readers and a previously committed writer need different patch
  bases.
- Added a controller-owned, head-checked `push` command after an interpolated
  PowerShell refspec was parsed as two refspecs, briefly deleting the PR branch.
  The branch and PR were restored immediately with no commit loss; the new push
  path passes one argument-array refspec, rejects head drift and fork heads, and
  verifies the resulting PR SHA. A unit test fixes the exact refspec contract.
- Added bounded read-after-write retries to the post-push PR-head check after
  Git accepted `492ac72` but the first PR GraphQL read still returned
  `b1f7072`. Only verification is retried; the push is never repeated. The
  push command also reconciles an already-visible harvested SHA without
  issuing another push. The reusable controller and renderer now have 14
  passing unit tests, including delayed-visibility, idempotence, and
  single-refspec contracts.
- Kept one canonical writer and host-only GitHub credentials; no second writer
  was needed.
- Added real failure harnesses, Compose rendering, Shellcheck, Actionlint, and a
  hosted PostgreSQL downgrade/re-upgrade gate because green unit tests alone did
  not exercise the review findings.
- The fourth cycle added explicit coverage for dangling symlink restoration,
  stale rollback-state admission, failed RunPod reconciliation, orphan service
  removal, and non-cancelable master deployment transactions.
- The fifth cycle proved why reviewer completion must precede the quiet window:
  Greptile completed after Cubic and found two new P1 ambiguity paths. The
  repairs preserve a possibly bound RunPod template after any attempted update
  and restore prior release files while leaving application services stopped
  when database downgrade fails.
- The sixth cycle extended the deferred-error rollback contract to application
  stop failure: prior files are restored, schema downgrade and restart are
  skipped, and the original stop status is returned for manual recovery.
- The unbounded trial was then stopped and converted to a coordinated policy:
  Greptile is the manually triggered primary reviewer, CodeRabbit and Cubic are
  advisory, findings are batched, and repair is capped at two batches. Commit
  `9db8405` disabled CodeRabbit incremental reviews and Greptile review-on-push;
  GitHub immediately reported `Review skipped: incremental reviews are
  disabled` rather than consuming/waiting for CodeRabbit quota.
- The final observed Greptile result before that policy change was 4/5 with one
  reproduced P1 and an explicit do-not-merge recommendation. The redesigned
  gate correctly reports that as a real blocker while treating CodeRabbit rate
  limits as non-blocking availability evidence.

## Recommended lane policy

Use one writer by default. Add one audit lane for workflow, security, or
configuration analysis and one test lane when its suite is independent and
long enough to overlap meaningfully. Beyond three total lanes, coordination and
compute cost are unlikely to pay back unless the repository has several long,
independent test partitions. Never add writer lanes for speed.

Reusable E2B aliases built during the evaluation:

- `pr-review-v1` (4 vCPU, 8 GiB), template `fxwye5wni5qrmtgg60o5`.
- `pr-review-audit-v1` (2 vCPU, 4 GiB), template
  `djgtg0a5ukhyagu1jeun`.

The reusable goal package is under `templates/pr-review-convergence/` and is
rendered by `ops/render_pr_review_goal.py`. Rendering validates the PR/repository
pair, requires explicit checks/reviewers/setup/validation commands, rejects a
non-empty destination, fails if any placeholder remains, copies the E2B runtime
tools and bootstrap/runbook documentation, and writes a SHA-256 package
manifest without secrets.

## Remaining evidence boundaries

E2B proved Linux CPU tests, shell failure paths, workflow syntax, and Compose
resolution. Hosted GitHub Actions independently proved the required CI,
including real PostgreSQL migration reversal. No test in this run changed a
live VPS, production database, GHCR deployment, or RunPod endpoint; those remain
external-operation boundaries rather than silently claimed validation.
