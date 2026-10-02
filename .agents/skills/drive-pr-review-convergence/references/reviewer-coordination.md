# Reviewer Coordination

## Roles

Use the workflow's independent whole-diff review as the primary final-candidate
review. CodeRabbit and Cubic are advisory inputs.

Greptile is retired for this project. Do not trigger it, request credentials, wait for its completion, or use its score/status as a readiness blocker. Independently validate any historical findings that still apply.

## Default schedule

1. Collect available human, CI, bot, and independent findings.
2. Deduplicate and reproduce before editing.
3. Push one coherent tested candidate and wait for required CI.
4. Complete independent review of the exact final head and affected boundaries.
5. If a reproduced P0/P1 remains, use the remaining repair batch and revalidate.
6. Stop blocked if a blocking issue remains after the second batch.
7. When clean, take two unchanged snapshots one minute apart and stop before merge.

Readiness requires no reproduced P0/P1 or required human finding, with evidence-backed
dispositions for every finding. Retired Greptile's score and completion do not gate readiness.

## CodeRabbit free-plan strategy

`.coderabbit.yaml` disables automatic incremental
reviews. CodeRabbit documents that incremental and manual review runs consume
the same rolling allowance, and its plan limits may change:
<https://docs.coderabbit.ai/management/plans>.

Record `rate-limited`, `skipped`, or `stale` as availability evidence and move
on. Never wait in a loop for quota. Do not manually request repeated full
reviews unless the user explicitly changes the reviewer budget.

## Finding ledger

Each row needs:

| Field | Meaning |
| --- | --- |
| `id` | Stable provider/thread/check identifier |
| `source` | Human, CI, CodeRabbit, Cubic, independent review, or historical findings from retired Greptile |
| `first_seen_sha` | Head SHA on which the finding appeared |
| `locator` | File/line, check, review body, or system boundary |
| `claimed_severity` | Provider rating, if any |
| `reproduced_severity` | Workflow's independently justified P0/P1/P2 |
| `disposition` | fixed, stale/already-fixed, not reproducible, false positive, duplicate, deferred follow-up, or blocked |
| `evidence` | Test, trace, code reasoning, or linked follow-up |

Never resolve a GitHub thread until its concern is fixed or disproved with
evidence.
