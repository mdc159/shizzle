# Reviewer Coordination

## Roles

Use one automated provider as the primary final-candidate reviewer. The default
policy uses Greptile because it performs a whole-diff review and exposes a
confidence score plus severity findings. Other bots, including CodeRabbit and
Cubic, are advisory inputs.

Reviewer bots do not control the agent and do not own the finding ledger. The
workflow's own whole-diff review defines the completeness boundary.

## Default schedule

1. Allow CodeRabbit's initial automatic review when available.
2. Collect all human, CI, Greptile, CodeRabbit, Cubic, and independent findings.
3. Deduplicate and reproduce before editing.
4. Push one coherent tested candidate.
5. Wait for required CI.
6. Trigger Greptile once on the final candidate.
7. If Greptile finds a reproduced P0/P1, use the remaining repair batch and one
   confirmation review.
8. Stop blocked if a blocking issue remains after the second batch.
9. When clean, take two unchanged snapshots one minute apart and stop before
   merge.

Greptile's `triggerOnUpdates` defaults to false and this project keeps it false
to prevent review-on-every-push loops. The manual trigger is `@greptileai`.
Greptile documents these controls at
<https://www.greptile.com/docs/code-review-bot/trigger-code-review> and the
`.greptile/` schema at
<https://www.greptile.com/docs/code-review/greptile-config-reference>.

## Greptile gate

- Target: 5/5 and no reproduced P0/P1.
- Acceptable exception: 4/5 only when no reproduced P0/P1 remains, every P2 is
  explicitly dispositioned, and the review does not recommend against merge.
- Not ready: 3/5 or lower, any reproduced P0/P1, a required human change
  request, or an explicit do-not-merge recommendation.

The score is evidence, not a substitute for reading findings.

## CodeRabbit free-plan strategy

`integrations/coderabbit/.coderabbit.yaml` disables automatic incremental
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
| `source` | Human, CI, Greptile, CodeRabbit, Cubic, or independent review |
| `first_seen_sha` | Head SHA on which the finding appeared |
| `locator` | File/line, check, review body, or system boundary |
| `claimed_severity` | Provider rating, if any |
| `reproduced_severity` | Workflow's independently justified P0/P1/P2 |
| `disposition` | fixed, stale, not reproducible, false positive, duplicate, deferred, or blocked |
| `evidence` | Test, trace, code reasoning, or linked follow-up |

Never resolve a GitHub thread until its concern is fixed or disproved with
evidence.
