# Execution Plan

1. Snapshot the remote PR head/base SHAs, branch protection, worktrees, checks,
   reviews, comments, and unresolved GraphQL review threads.
2. Run the package bootstrap, verify the support boundary in `ENVIRONMENT.md`,
   and provision one writer pinned to that head with the package `setup.sh`.
   Add only reader lanes justified by independent parallel work. Record
   template, setup-script digest, sandbox IDs, lifecycle, and tool versions.
3. Collect one intake round, build a deduplicated finding ledger, and
   independently review the whole diff. Do not push comment-by-comment fixes.
4. Separate autonomous in-scope corrections from decisions requiring approval.
5. Implement one coherent candidate in an ignored staging worktree and replay it
   into the writer/readers from the recorded head.
6. Run focused checks, then `{{VALIDATION_COMMANDS}}`, plus a failure matrix for
   any changed deployment, migration, concurrency, security, or state boundary.
7. Commit in the writer, harvest a verified bundle, inspect the imported host
   ref, reconfirm the unchanged remote head, and push without force.
8. Reply to each finding with its exact disposition and evidence; resolve only
   fixed, disproved, duplicate, or explicitly deferred conversations.
9. After required CI passes, trigger `{{PRIMARY_REVIEWER}}` once on the final
   candidate. Treat `{{ADVISORY_REVIEWERS}}` as non-blocking inputs and never
   wait for their quota. Repeat only for a reproduced P0/P1, up to
   `{{MAX_ITERATIONS}}` total repair batches.
10. Once clean, capture two unchanged snapshots
    `{{QUIET_WINDOW_MINUTES}}` minutes apart. Report the exact SHA, checks,
    reviewer state, disposition counts, validation, residual risks, sandbox
    state, and recommended merge method. Stop before merge.
