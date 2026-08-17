# Troubleshooting

## `E2B_API_KEY is not present`

Check only existence with `Test-Path Env:E2B_API_KEY` or
`test -n "${E2B_API_KEY:-}"`. If the key was added after Codex Desktop or the
terminal started, restart the process. Never print the value.

## E2B doctor hangs or fails

Confirm internet access, account/quota status, and pinned SDK compatibility.
`doctor` lists at most one sandbox as a connectivity probe; it does not create
or destroy a sandbox.

## Private clone fails

Expected behavior. Use the reviewed host-bundle design in
`private-repositories.md`; do not paste a personal access token into the setup
script.

## Setup fails after clone

Inspect the preserved `paused-failed` run record and sandbox. Ensure setup is
Linux-compatible, non-interactive, credential-free, and uses lockfiles. Rebuild
the E2B template only when the base toolchain—not ordinary project
dependencies—needs to change.

## `an active writer already exists`

Inspect `list` and `info --refresh`. Resume or deliberately destroy the old
writer after preserving artifacts. Do not bypass the invariant by renaming a
second writer as an audit lane.

## `source worktree must be beneath ...staging`

Create the staging worktree under the package's exact
`.sandbox/e2b/staging/` directory. This path restriction prevents accidental
capture of unrelated worktrees.

## Remote head changed

Stop, fetch, rebuild the finding ledger, and rebase/replay deliberately. Never
force-push stale sandbox work.

## Bundle harvest fails

The writer must have a clean worktree and at least one commit beyond the
recorded base. Commit or remove untracked files, rerun validation, then harvest.

## Push succeeded but first read is stale

The controller retries only read-after-write verification. It never repeats the
push. If the harvested SHA is already visible, the operation reconciles
idempotently.

## CodeRabbit says rate limited or skipped

Record the provider result and continue. Do not wait or repeatedly trigger.
Only required CI and the configured primary final review gate readiness.

## Greptile has a score but reviewed an older commit

Not a final review. Wait for or manually trigger one review on the tested final
candidate, then confirm its reviewed head and findings.

## The PR is green but a review says do not merge

Green status is only one evidence input. Reproduce the stated finding. A real
P0/P1 or required human change request blocks readiness regardless of the green
badge.
