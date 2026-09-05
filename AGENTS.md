# Agent Entry Point

Read this before changing anything in this repository, whatever runtime you
are (Claude, codex, pi, gemini, or other).

## What this is

Shizzle is a karaoke pipeline: cloud GPU stem separation (RunPod) hands a
defined lossless package to a VPS delivery pipeline that publishes browser
tracks at https://shizzle.systems. Current state and next actions:
[docs/HANDOFF.md](docs/HANDOFF.md).

## Ground rules

1. **Invariants are law.** [docs/INVARIANTS.md](docs/INVARIANTS.md) holds 48
   numbered contracts (A1–F6). A PR that changes an invariant must update
   that file and its guarding test in the same PR. The AI reviewers cite
   these IDs; so should you.
2. **Master is protected.** Repository policy is that all work lands by PR
   through four required checks (`library`, `stemsplit`, `player`,
   `postgres-contract`). The current branch rule enforces those checks for
   administrators but does not itself require pull requests. Greptile,
   CodeRabbit, and cubic are advisory to branch protection; the convergence
   workflow below still requires a current-head Greptile review.
3. **Green badges are not readiness.** PR readiness is judged by the
   `drive-pr-review-convergence` workflow — installed at
   `.agents/skills/drive-pr-review-convergence/`, launched via the
   repo-local goal `goals/pr-review-convergence-loop/goal.md`. It builds a
   finding ledger from all reviewer output, independently reviews the diff,
   and gates on Greptile 5/5 with a quiet window. It stops before merge.
4. **Merging master deploys production.** Every master merge builds a
   digest-pinned api image and queues a transactional VPS deploy behind a
   human-approved environment gate. Understand
   [docs/AUTOMATION.md](docs/AUTOMATION.md) before touching
   `.github/workflows/` or `deploy/` — it documents the secrets, the
   failure modes, the rollback paths, and the traps already paid for.

## Map

- `library/` — FastAPI control plane, orchestrator, publisher, alembic
  migrations (the single schema writer).
- `stemsplit/` — RunPod GPU worker producing the `lossless-stem-v1` package.
- `player/` — browser UI (vite).
- `deploy/vps/` — production compose, Caddyfile, transactional deploy and
  restore scripts plus their fault-injection tests.
- `deploy/runpod/` — serverless endpoint assets.
- `interfaces/` — the frozen interface specs between subsystems.
- `docs/` — runbooks; start with HANDOFF.md, then AUTOMATION.md.

## Cross-repo knowledge

Portable lessons and fleet coordination live in the
`mdc159/agent-knowledge-exchange` repo (locally `D:\agent-knowledge-exchange`)
— see `knowledge/deployment/gated-deploy-first-run-lessons.md` for the
hard-won history behind this repo's deploy pipeline. Reusable skills come
from the Capability Library (`D:\Projects\Capability-library`, installed
per-project into `.agents/skills/`).
