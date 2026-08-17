# Repository Adaptation Checklist

Before rendering:

- Confirm the PR URL, repository, PR number, base branch, and current head SHA.
- Identify branch-protection-required checks by exact status-check name.
- List the actual reviewer GitHub Apps installed and authorized for the repo.
- Select exactly one primary reviewer.
- Decide the maximum repair batches; default to two.
- Identify all languages, package managers, lockfiles, generated sources, and
  platform-specific tests.
- Write credential-free, deterministic, non-interactive Linux setup commands.
- Write focused and full validation commands with no production side effects.
- Identify migrations, rollback, concurrency, security, workflow, deployment,
  or state boundaries needing a failure matrix.
- Decide whether public credential-free clone is sufficient.
- Decide whether one writer alone is sufficient; justify every reader.

After rendering:

- Read the rendered `goal.md`, `facts.md`, `plan.md`, and `review-policy.json`.
- Inspect `setup.sh`; require locked versions and no secret access.
- Copy `.gitignore.snippet` into the target repository's `.gitignore`.
- Run the appropriate bootstrap script.
- Build or select the declared E2B template alias.
- Confirm the rendered manifest hashes.
- Scan for unrendered placeholders and credential assignments.
- Launch `/goal` with the rendered `goal.md`.

Before declaring ready:

- Reconcile every review thread, review body, issue comment, and check.
- Verify the final head SHA, required CI, primary review, findings, and quiet
  window all refer to the same commit.
- State unavailable validation and advisory reviewer status as residual risk.
- Recommend a merge method from actual history, then stop before merge.
