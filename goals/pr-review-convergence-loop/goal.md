# Goal: Drive One Pull Request to Verified Review Convergence

Target pull request: **Provide one GitHub PR URL or an unambiguous repository and PR number when launching this goal.**

Actively drive the target pull request to a genuinely clean, evidence-backed, ready-to-merge state. Use `ops/e2b_pr_sandbox.py` to create one canonical writer sandbox for the PR and, when useful, separate read-only audit or test lanes pinned to the same remote head. The workflow performs its own independent whole-diff review; CodeRabbit and Cubic are advisory inputs. Collect and independently validate their findings, repair them in coherent batches, and never wait for an advisory review quota.

Use [facts.md](facts.md) as the shared acceptance contract and [plan.md](plan.md) as the authoritative execution and verification plan. Preserve unrelated work; do not blindly implement review suggestions; pause for scope-expanding, security-semantic, destructive, permission, or product decisions; and never merge, force-push, close, or retarget the PR without explicit user approval.

For use outside this checkout, first render the self-contained package from
`.agents/skills/drive-pr-review-convergence/assets/pr-review-convergence/` with
`.agents/skills/drive-pr-review-convergence/scripts/render_pr_review_goal.py`. Launch
the rendered `goal.md`, not this repository-specific source goal. The rendered
package carries its E2B tools, host bootstrap checks, API-key and support
contract, operating runbook, project setup, ignore rule, and integrity manifest.

Use at most two repair batches. CodeRabbit receives its automatic first-pass
review when available, but no incremental reruns and no quota waiting.
Greptile is retired for this project. Do not trigger it, request credentials, wait for its completion, or use its score/status as a readiness blocker. Independently validate any historical findings that still apply.

The `facts*.json`, `interview*.json`, and `evaluation-pr4.md` files are historical
capture artifacts. Current reviewer policy is defined by this goal, `facts.md`,
and `plan.md`; retired Greptile's historical requirements do not apply.

## Done condition

This goal is complete only when the exact latest PR head SHA has all required checks green, no reproduced P0/P1 or required human finding remains, every finding has an evidence-backed disposition, and the independent whole-diff review and failure matrix pass. Retired Greptile's review and score do not gate readiness. CodeRabbit or Cubic being rate-limited, skipped, or stale is disclosed but does not block. Two unchanged polls one minute apart close the gate. Report `READY TO MERGE` but stop before merging.
