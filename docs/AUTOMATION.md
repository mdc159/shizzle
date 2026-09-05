# Shizzle Automation

The review-and-deploy automation package: CI gates, image publishing, gated
VPS deploys, and how the three AI reviewers stay aligned on
[INVARIANTS.md](INVARIANTS.md). This document describes the checked-in workflows under `.github/workflows/`.
GitHub branch rules, environment reviewers, secrets, and live endpoint settings
are configured outside these files and must be verified when bootstrapping.

## 1. System overview

```mermaid
flowchart TB
    PR["PR to master"] --> CHECKS["ci.yml: library, stemsplit,<br/>player, postgres-contract"]
    PR --> REVIEW["Advisory review intake +<br/>PR convergence workflow"]
    CHECKS --> MERGE["Human merge"]
    REVIEW --> MERGE
    MERGE --> MASTER["Push to master"]
    MASTER --> PUSHCI["Same four CI checks"]
    MASTER --> CLASSIFY{"deploy-gate:<br/>any non-documentation path?"}
    CLASSIFY -->|"No"| SKIP["Skip API build and VPS deploy"]
    CLASSIFY -->|"Yes, or unknown diff"| DEPLOY["deploy-production<br/>waits for four checks + gate"]
    PUSHCI --> DEPLOY
    DEPLOY --> ADMIT["deploy-vps: verify current master SHA"]
    ADMIT --> IMAGE["api-image: build and push tag + digest"]
    IMAGE --> APPROVAL["production environment gate<br/>configured in GitHub"]
    APPROVAL --> RECHECK["Recheck master; build player; ship assets"]
    RECHECK --> TX["Snapshot files + DB revision;<br/>pull, validate, migrate, start"]
    TX --> HEALTH{"Public root + health +<br/>unauthorized /cdn = 403"}
    HEALTH -->|"Pass"| FINAL["Finalize transaction; retain images"]
    HEALTH -->|"Fail"| ROLLBACK["Restore prior schema + files;<br/>verify restored health"]
    TX -->|"Transaction failure"| ROLLBACK
    MASTER --> FILTER{"Worker paths changed?"}
    FILTER -->|"Yes"| WORKER["worker-image: publish SHA-tagged worker<br/>independent of application CI"]
    MANUALBUILD["Manual worker-image dispatch"] --> WORKER
    WORKER -. "operator selects published tag" .-> REPOINT["Manual runpod-repoint:<br/>resolve digest, clone template,<br/>update endpoint + worker limit"]
```

The required check names are `library`, `stemsplit`, `player`, and
`postgres-contract`. AI review output is advisory to branch protection; the
repository's [convergence workflow](../.agents/skills/drive-pr-review-convergence/SKILL.md)
separately determines review readiness and stops before merge.

Only a deployable master push passing all four checks calls `deploy-vps.yml`.
Documentation-only changes still run checks. `worker-image.yml` has its own
path filter (`stemsplit/**` or its workflow file) and manual trigger; it neither
waits for those checks nor updates an endpoint. `runpod-repoint.yml` is
manual-only, uses a registry digest, clones the endpoint's current template,
and sets the new template plus `workers_max` in one endpoint update. The prior
template remains available for explicit rollback. After an ambiguous endpoint
update, the new template is preserved pending reconciliation.

## 2. Secrets inventory

| Name | Scope | Grants | Rotation |
|------|-------|--------|----------|
| `VPS_SSH_KEY` | Environment `production` | SSH private key for root on the VPS host; the workflow runs SSH and Compose as root | Generate new keypair, install in `~/.ssh/authorized_keys`, update the secret, remove old pubkey |
| `VPS_HOST` | Environment `production` | Hostname/IP the deploy targets | Update when the box moves |
| `VPS_SSH_HOST_KEY` | Environment `production` | Pinned ed25519 host key so deploy-vps verifies the box (no `StrictHostKeyChecking=no`) | Regenerate with `ssh-keyscan -t ed25519 <host>`; update after any host key change |
| `RUNPOD_API_KEY` | Repo | RunPod API access for `runpod-repoint.yml` (create templates and update endpoints); image publishing uses `GITHUB_TOKEN` | Rotate in the RunPod console, update the repo secret |

Runtime secrets (passcode, `POSTGRES_*`, `AWS_*`, CloudFront private key) are
NOT GitHub secrets — they live only in the gitignored `.env` and mounted files
under `/opt/shizzle/prod` per invariant E3. The orchestrator defaults to
`SHIZZLE_PIPELINE=cloud`; with `RUNPOD_API_KEY` and `RUNPOD_ENDPOINT_ID` unset
the stack runs in the valid parked-cloud state — the orchestrator starts and
its heartbeat keeps /api/health green, and new jobs fail closed at dispatch
with `RUNPOD_DISPATCH_FAILED`. A job already dispatched to RunPod when
credentials disappear parks instead: its polls fail retryable and the stall
watchdog does not fire while the client is unconfigured, so the live remote
job reconciles on the first poll after credentials return. Set both
variables in the production `.env` when the RunPod path is connected.

## 3. Deploy procedure

**On merge to master:**

0. The `deploy-gate` job classifies the merge by diffing it against its
   parent. If every changed file is documentation (`docs/`, `evidence/`,
   `goals/`, `.agents/`, or any `*.md`), the deploy chain is skipped —
   nothing a docs-only merge ships would differ, so building an image and
   queuing a production approval is skipped. The gate
   fails open: a missing parent or empty diff deploys.
1. After all four jobs pass on a `master` push, `ci.yml` calls
   `deploy-vps.yml`; pull-request and non-master runs cannot call it. The called
   workflow rechecks the supplied SHA against current `master`. Job
   `build-image` calls reusable `api-image.yml`, which builds and pushes the
   SHA tag and exposes its registry digest.
2. Job `deploy` pauses at the GitHub Environment `production` gate until
   mdc159 approves. The workflow rechecks that `master` still names the same
   reviewed SHA after approval; if `master` advanced while approval was pending,
   the stale run exits cleanly without shipping.
3. After approval, the deploy SSHes to the VPS (using the pinned host key),
   records the prior files and database revision, sets
   `SHIZZLE_API_IMAGE=<tag>@<digest>`, validates the staged Compose config,
   runs `alembic upgrade head` explicitly from the new api image, and starts
   the services. The box receives no source code.

**Health checks after deploy** — the deploy-vps.yml `Check production health`
step asserts, retrying up to 12 times at 10 s intervals until all pass: the
public root request succeeds, `/api/health` passes a jq assertion
(`status=="ok"`, `db==true`, `orchestratorAlive==true`), and `/cdn/tracks/x`
returns exactly 403 (media requires auth). The broader manual verifications
in `deploy/vps/README.md` (telemetry endpoint, CloudFront media Range
behavior) are NOT asserted by the workflow.

**Rollback:**

- Automatic: a deploy or health failure downgrades Alembic to the recorded
  prior revision before restoring the complete prior file snapshot and image
  identity. A real downgrade is mandatory under F1. If the downgrade itself
  fails, rollback still restores the prior files but leaves API and
  orchestrator stopped and returns the original downgrade failure for manual
  recovery; it never starts an old image against an unknown schema state. A
  failed application stop likewise restores the files, skips downgrade and
  restart, and returns the stop failure because schema mutation is unsafe while
  the new application may still be running.
- Break-glass: follow the schema-aware recovery procedure in
  [the VPS runbook](../deploy/vps/README.md). An older image cannot safely start
  until the database revision is compatible with that image. Automated deployment
  refuses an installation with no prior API identity; bootstrap the first
  release explicitly.

**Local-build fallback:** when GHCR is unreachable or for one-off debugging,
the `compose.build.yml` overlay builds the api image on the box from a source
copy instead of pulling the digest-pinned image. Set
`SHIZZLE_API_IMAGE=shizzle-api:local-build`; the overlay points both api and
orchestrator at that one build. It is a fallback, not the
normal path — deploys never ship source to the box.

## 4. Reviewer alignment

All three reviewers consume the same source of truth:
[INVARIANTS.md](INVARIANTS.md).

- **CodeRabbit** — configured by [`.coderabbit.yaml`](../.coderabbit.yaml):
  `path_instructions` mirror the invariant series per area, and the
  `knowledge_base.code_guidelines` block makes CodeRabbit ingest
  `docs/INVARIANTS.md` directly, so reviews cite invariant IDs ("violates B3").
- **Greptile** — the primary whole-diff validator. `.greptile/config.json`
  limits it to logic/syntax findings, points it at repository context, and
  disables review-on-every-push. `.greptile/files.json` supplies this document
  and `INVARIANTS.md` as canonical context. Trigger the final review manually
  only after a coherent candidate passes CI and failure-path validation.
- **cubic** — configured through its dashboard custom rules. The seven paste
  texts below are the standalone versions of the same path instructions; the
  owner pastes each into the dashboard.

### Coordinated review policy

- CodeRabbit is a free-plan advisory intake reviewer. It reviews the initial PR
  when quota is available; automatic incremental review is disabled. Never wait
  for quota or require a final-head CodeRabbit run.
- Cubic is advisory. Consume available P0/P1 findings, but do not wait for its
  completion or restart the loop for P2 polish.
- Greptile is primary. After batching all validated intake findings and passing
  required CI, manually trigger one final whole-diff review. Target confidence
  5/5. Confidence 4/5 is acceptable only when there are no reproduced P0/P1
  findings and every P2 has an explicit disposition. A score of 3/5 or lower,
  or Greptile's explicit do-not-merge recommendation, is not ready.
- Allow at most two repair batches. If a reproduced P0/P1 remains after the
  confirmation review, stop for human adjudication instead of greplooping.
- Reviewer findings are deduplicated into one ledger and fixed in batches. The
  coordinator owns readiness; reviewers provide evidence and never command one
  another.

## 5. cubic paste-text

One block per path scope; paste each into cubic's dashboard custom rules.

```text
Scope: stemsplit/**
RunPod GPU worker for the lossless-stem-v1 interface. Enforce handoff-last
ordering (A1): handoff.json must be uploaded only after every stem PUT has
returned — flag any code path that writes or renames it earlier. Every dispatch
attempt must write beneath its own immutable attempts/<sha256(idempotency_key)>
prefix (A6); older workers must never clobber newer receipts. Check S3
atomicity assumptions (A1, A10): every upload carries a locally computed
sha256, and the shared progress callback closure is mutated by several
transfer-manager threads, so every read-modify-write must hold its lock and
progress must stay monotonic (A11). Stems must stay pcm_f32le stereo 44100
with identical sample counts and no per-stem normalization (A3). The
flat-module layout (lossless_worker.py, s3_ops.py, etc. as siblings, no
package structure) is intentional for the worker image — do not suggest
packaging or directory reorganization.
```

```text
Scope: library/src/shizzle_server/orchestrator/**
Durable orchestrator stage loop. Enforce the B-series invariants from
docs/INVARIANTS.md: lease ownership must be checked inside the locked
transaction (B2), and the dispatch reservation must commit before any external
RunPod call (B3) — a timeout or 5xx must never trigger a second dispatch; it
reconciles the outstanding reservation instead. Poll-failure events dedupe to
one row per outage and any other event resets the window (B6). A failed RunPod
cancel must never mask the original error — _cancel_best_effort logs and
swallows (B7). Park frees the lease without consuming an attempt or appending
an event (B9). Every unresolvable error path fails closed (B5, B10) and every
stage handler must be idempotent under crash-rerun (B11). Heartbeats are
written only on phase change (B8); a RunPod job already marked failed
dispatches fresh under a new idempotency key (B12).
```

<!-- Mirrored copy: duplicates the library/src/shizzle_server/db/repository.py
     path instruction in .coderabbit.yaml — edit both together. -->
```text
Scope: library/src/shizzle_server/db/repository.py
Postgres data layer for the orchestrator and publisher. Enforce the
data-layer invariants from docs/INVARIANTS.md: job claims use
SELECT ... FOR UPDATE SKIP LOCKED and reclaiming an expired foreign lease
records a lease_reclaimed event in the same transaction (B1); lease
ownership must be verified under row lock (with_for_update) inside the same
transaction as every lease-scoped write (B2); the dispatch reservation must
be committed by its own transaction before any caller performs the external
API call, and duplicate reservations must be blocked (B3); confirmation
deliberately requires no lease — key it on the latest reservation so a stale
dispatcher cannot overwrite a newer one (B4); legacy dispatch_unconfirmed
state must fail closed, blocking redispatch rather than paying twice (B5);
worker heartbeats are written only on phase change (B8); park frees the
lease without consuming an attempt or appending an event (B9). Publish
writes are idempotent: track ids are deterministic uuid5 so crash-reruns
converge on one row (C4), and generation activation is a compare-and-swap
under with_for_update with the ledger event committed in the same
transaction (C5). Job events are append-only and ordered — no UPDATE or
DELETE on the events table (F5).
```

```text
Scope: library/src/shizzle_server/publish/**
Publication and delivery-profile policy. Enforce the C and D series from
docs/INVARIANTS.md: the manifest is the completion marker and is written last,
via server-side copy only (C2); published generations are immutable — an
existing destination manifest makes the publish a no-op (C1); the format guard
(m4a stems, 64 MiB cap, no raw PCM) must run before any promotion (C3); track
ids are deterministic uuid5 so reruns converge (C4); generation activation is
compare-and-swap with the ledger event in the same transaction (C5).
Duration/bitrate gates must reference the named constants in
delivery_profile.py (TRACK_DURATION_TOLERANCE_SEC 0.100,
STEM_DURATION_TOLERANCE_SEC 0.080, STEM_INTER_DURATION_TOLERANCE_SEC 0.005,
2.5 Mb/s cap) — flag any magic number or locally redefined tolerance (D1-D4).
delivery_profile.py must stay a pure policy module with no boto3/ffmpeg/db
imports (D7); at most one common attenuation, never per-stem and never a
boost (D3).
```

```text
Scope: library/alembic/**
Database migrations. Enforce the F series from docs/INVARIANTS.md: a single
linear chain with numeric filename prefix equal to the revision id and an
explicit down_revision (F1); additive-only changes unless the author
explicitly signs off on a destructive or rewriting migration — if you see a
drop/rewrite, ask for that sign-off in review. Every upgrade needs a paired,
real (non no-op) downgrade (F1). The DSN comes from DATABASE_URL and
target_metadata is Base.metadata (F2). Only the explicit production deploy
transaction migrates the production database from the api image after recording
the rollback revision (F3); long-running api and orchestrator services never
migrate. Isolated contract databases may run alembic upgrade head. The migration
itself is under test via the contract suite's real `alembic upgrade head`
subprocess (F4) — flag any test fixture that uses create_all.
```

```text
Scope: .github/workflows/**
CI/CD workflows. Build tags must be sha-pinned and deployments must use the
registry digest; flag latest/vN tags, tag-only deploys, or hand-pushed images.
Every workflow needs least-privilege permissions and a concurrency group. No
secret echoing or StrictHostKeyChecking=no. deploy-vps admits only the current
master SHA after successful push CI, rechecks it after the production approval
gate, and must roll back both files and the database revision on failure.
```

```text
Scope: player/**
Browser player. Light review: correctness and obvious accessibility issues
only. No styling bikeshed — do not comment on CSS preferences, formatting, or
component organization. Flag real bugs (state races, broken playback logic,
unhandled errors) and clear WCAG violations (missing labels, keyboard traps,
contrast only when egregious).
```

## 6. Verification and remaining coverage limits

The `library` CI job runs both shell failure-path suites with stubbed Docker,
registry, and RunPod calls. The required `postgres-contract` job uses a real
Postgres service, applies Alembic migrations, exercises concurrency/crash
contracts, and downgrades the head migration by one revision before upgrading
again. The `player` job builds, lints, and runs only the mocked
`e2e/library-scroll.spec.ts` browser test. These checks do not replace the
[real playback tests](playback-troubleshooting.md) or prove live deployment
rollback against a real Docker stack.

Current checked-in limits:

- `mypy src` in the library job is informational (`continue-on-error: true`).
- Player `knip` is configured but not run in CI.
- The player check installs Chromium from the Playwright browser CDN on each
  run; that dependency can block CI during an outage.
- CPU worker tests mock separation; the image's offline weights check proves
  model availability, not successful GPU separation or runtime egress policy.
- RunPod repoint tests mock the API, so live template/endpoint API behavior
  requires a separate authorized validation.
- The worker-image workflow lacks a concurrency group and uses version-tagged
  Actions, unlike the SHA-pinned application image/deploy workflows.
- See [INVARIANTS.md](INVARIANTS.md) for explicitly unguarded contracts.

## 7. Installation prerequisites

Set up these external dependencies before the first automated deployment:

1. Create and configure the GitHub `production` environment and its required
   reviewer before allowing deployment runs. Referencing an environment in
   YAML does not itself configure an approval requirement.
2. Add the three VPS environment secrets from §2, plus the repository RunPod
   secret when endpoint updates are needed. The CI caller currently passes
   `secrets: inherit` to the deploy workflow.
3. Make the GHCR API/worker packages readable by their target hosts: public
   packages or explicit read credentials. A successful Actions push does not
   prove that an anonymous VPS or RunPod pull succeeds.
4. Bootstrap the VPS files, mounted secrets, external Caddy volume, database
   revision, and first active image as described in
   [deploy/vps/README.md](../deploy/vps/README.md). Automated deployment requires
   a real prior release to restore.
5. Configure branch protection around the actual four CI check names and the
   repository's PR policy. Configure the advisory reviewers using §4–5.
6. Configure and independently verify the RunPod endpoint using
   [deploy/runpod/README.md](../deploy/runpod/README.md). Image publication and
   endpoint activation are separate operations.
