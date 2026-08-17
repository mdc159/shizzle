# E2B PR Review Operations

Run these commands from the rendered package directory. Replace only values in
angle brackets. PowerShell examples use the package's absolute path so the
controller's state and staging-worktree boundary remain stable.

## 1. Preflight and template

```powershell
$Package = (Get-Location).Path
.\bootstrap.ps1
uv run .\tools\e2b_pr_template.py --alias pr-review-v1 --tier standard
```

Build the template once per alias or whenever the base toolchain changes. Edit
and test `setup.sh` whenever the target project's dependency bootstrap changes.

## 2. Create exactly one writer

```powershell
uv run .\tools\e2b_pr_sandbox.py create `
  --repo {{OWNER/REPO}} --pr {{PR_NUMBER}} --role writer `
  --template pr-review-v1 --setup-file "$Package\setup.sh"
```

The command records the exact PR head, clones it without credentials, runs the
setup file, and pauses the sandbox after provisioning. Save the returned
`run_id`. Controller records and artifacts live below `.sandbox/e2b/`.

## 3. Optional independent readers

One sandbox is sufficient for correctness. Use fanout only when lanes can run
in parallel against the same candidate:

```powershell
uv run .\tools\e2b_pr_sandbox.py fanout `
  --repo {{OWNER/REPO}} --pr {{PR_NUMBER}} `
  --roles audit-security,test-integration `
  --template pr-review-v1 --setup-file "$Package\setup.sh"
```

Readers cannot harvest or push. Do not create a second writer.

## 4. Inspect and execute

```powershell
uv run .\tools\e2b_pr_sandbox.py list
uv run .\tools\e2b_pr_sandbox.py info <RUN_ID> --refresh
uv run .\tools\e2b_pr_sandbox.py resume <RUN_ID>
uv run .\tools\e2b_pr_sandbox.py exec <RUN_ID> -- git status --short
uv run .\tools\e2b_pr_sandbox.py exec <RUN_ID> -- <VALIDATION_COMMAND>
uv run .\tools\e2b_pr_sandbox.py pause <RUN_ID>
```

`exec` does not invoke an interpolated host shell. Everything after `--` is the
remote command and its arguments.

## 5. Stage and replay candidate changes

Create the local Git worktree beneath the controller's required staging root:

```powershell
$Stage = "$Package\.sandbox\e2b\staging\pr-{{PR_NUMBER}}"
git worktree add --detach "$Stage" <EXACT_PR_HEAD_SHA>
```

Make and validate candidate edits there. Synchronize its binary-safe diff into
the writer, then into any readers:

```powershell
uv run .\tools\e2b_pr_sandbox.py sync-diff <WRITER_RUN_ID> "$Stage"
uv run .\tools\e2b_pr_sandbox.py sync-diff <READER_RUN_ID> "$Stage"
```

For a later repair batch based on a committed candidate, use `--base-ref` and,
for a disposable reader, `--replace`. Read `--help` before using those modes.

## 6. Commit, harvest, and host push

Commit inside the writer only after validation. The sandbox must be clean for
harvest:

```powershell
uv run .\tools\e2b_pr_sandbox.py exec <WRITER_RUN_ID> -- git status --short
uv run .\tools\e2b_pr_sandbox.py harvest <WRITER_RUN_ID>
```

Harvest verifies the Git bundle, imports it under
`refs/sandbox/e2b/<RUN_ID>`, and proves ancestry from the expected remote head.
Inspect that ref and the whole diff on the host. Only after explicit permission
to update the PR branch:

```powershell
uv run .\tools\e2b_pr_sandbox.py push <WRITER_RUN_ID> --remote origin
```

The push is non-force, checks the PR head immediately before writing, then
reads the head back. Never replace this command with a shell-interpolated
refspec.

## 7. Review convergence and cleanup

Follow `plan.md` and `review-policy.json`: batch findings, independently
reproduce them, wait for required CI, trigger the primary reviewer only on the
final candidate, and perform two unchanged quiet-window observations. Advisory
reviewer quota never blocks the loop. Stop at the repair-batch limit or before
merge.

Pause preserves a sandbox. Destroy is permanent and requires the exact run ID:

```powershell
uv run .\tools\e2b_pr_sandbox.py pause <RUN_ID>
uv run .\tools\e2b_pr_sandbox.py destroy <RUN_ID> --confirm <RUN_ID>
```

Remove a local staging worktree only after resolving its absolute path and
confirming it is beneath `$Package\.sandbox\e2b\staging`.

## Recovery rules

- If the remote PR head changed, stop; do not replay or force-push stale work.
- If provisioning fails, inspect the durable run record and paused-failed
  sandbox before retrying.
- If a reviewer is rate-limited, record it; only primary-reviewer completion
  gates readiness.
- If the iteration limit is reached with reproduced P0/P1 findings, report
  blocked with evidence. Never turn the limit into a readiness waiver.
- Never merge, close, retarget, or resolve a disputed finding without the
  authorization required by `goal.md`.
