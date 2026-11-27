# Worker nodes: the original plan (archived)

This is the planning record behind [Worker nodes](../WORKER-NODES.md),
written 2026-11-05 against master `0415ed2` and implemented in #63. It is
kept for its reasoning and the owner's decisions; it does not describe the
running system. For what runs today, read [Worker nodes](../WORKER-NODES.md)
and [`node/README.md`](../../node/README.md).

Resolved since: the open question below was settled on 2026-11-05 by an
inventory of the Dell 9530. Its only Shizzle image is the production lossless
worker (`ghcr.io/mdc159/shizzle/worker`, built 2026-09-24, GPU-proven on the
RTX 4070), with no downloader and no all-in-one container. The node design
therefore applied unchanged, and the Dell runs the agent in a container built
on that image (see `node/README.md`).

## Open question 2026-11-05: which container runs on the Dell

Owner, 2026-11-05: "For creating the files, we've got that nailed down. We
shouldn't change anything. We put a lot of work into optimizing the
multi-track file." Later: "The 6 should be named shizzle.m4a, not other."

This plan assumes the Dell runs the production lossless worker image
(`ghcr.io/mdc159/shizzle/worker`, `stemsplit/Dockerfile.lossless`), which is
what RunPod runs today. Under that assumption nothing about file creation
changes: the node emits the lossless package (six WAVs, the sixth named
`stems/shizzle.wav`) and the VPS derives the browser files, with the sixth AAC
stem named `stems/shizzle.m4a`, exactly as it does for a browser upload now.

Two facts do not fit that assumption and are unresolved from this session:

- The web path has never produced `multi-track.mp4` (open issue #29 is that
  the website's Download Multi-Track button cannot succeed for cloud
  uploads). The only code in the four reviewed repos that writes
  `multi-track.mp4` is the retired all-in-one worker in
  `mdc159/shizzle-worker`, which also names the sixth stem `other.m4a` and
  encodes stems at 320 kb/s.
- The owner reports a local container that downloads, splits and writes the
  optimized multi-track file, with the sixth stem named `shizzle.m4a`. That
  combination matches no code in the reviewed repos.

Until the Dell's image is identified (`docker images`, `docker ps -a`, or a
Remote Control session), the node design stays as written below. If the Dell
runs a newer all-in-one container that emits finished browser media, the
handoff would move to the drop-box (`imports/{source_ref}/`, invariant C8),
which requires a complete `shizzle-browser-v1` package (every v3 manifest
field, not just `delivery_profile` and `integrity.objects`), and the 2.5 Mb/s
budget (D4) would have to be measured on one real package first. That path is
not implemented; the Mac inventory found no such container. If it runs the production worker
image, the plan applies unchanged and a multi-track archive file is an
optional add-on step on the node, assembled from finished stems without
re-encoding.

## Goal

The owner, 2026-11-05:

> "Have the cloud version working and using the Mac Mini and the Dell 9530
> as worker nodes. If I wanted to drop a URL in there, it checks to see from
> the web if there's a worker node available and uses one of those devices
> to download. If that node is also capable of doing the stem splitting, it
> does that as well to take the load off the serverless. The serverless only
> operates when there is no node available."

Restated as rules the orchestrator will enforce:

1. The web app at <https://shizzle.systems> stays the one control plane:
   one catalog, one job ledger, one publication path, one set of gates.
2. A URL job is downloaded by an online node. The VPS never runs yt-dlp
   (datacenter addresses are blocked; that is why `YTDLP_BLOCKED` exists).
   If no download-capable node is online, the job waits for one.
3. Separation goes to an online, idle, separation-capable node first.
   RunPod is used only when no such node answers within a short window.
4. Nodes hand off exactly what RunPod hands off today: a `lossless-stem-v1`
   package under the job's attempt prefix, `handoff.json` last. The VPS
   verifies, derives browser media and publishes exactly as it does now.
   Nothing after the interface changes.
5. A node that goes away mid-job is treated like a dead RunPod worker: the
   attempt fails, the job redispatches under a fresh key, and the next
   executor may be another node or RunPod (invariant B12).

## What exists today and what is reused

| Need | Exists | Where | Reuse |
|---|---|---|---|
| URL job creation | yes, unused by the UI | `POST /api/submit-url` in `library/src/shizzle_server/api/routes.py` | keep; wire the player to it |
| URL download | yes, Mac only, not integrated | `ingest/` on `cbassist/m-shizzle` branch `codex/mac-youtube-ingest`: a Docker image with yt-dlp, ffprobe, sha256, local queue | the node's download container, used as is |
| Separation image | yes, in production | `ghcr.io/mdc159/shizzle/worker` from `stemsplit/Dockerfile.lossless`, already has `--local` mode | the node's separation container, same image RunPod pulls |
| Attempt claim, receipt heartbeat, handoff-last, post-write verification | yes, RunPod-bound | `stemsplit/lossless_handler.py` `handler()` | extract into an executor module both RunPod and nodes call |
| Mac GPU separation proof | yes | `evidence/spikes/mac-stem-separation/` on the Mac fork: 255.9 s track separated in 18.2 s on `mps:0` | evidence that rule 3 is worth doing |
| Dispatch reservation, confirmation, reconciliation, stall watchdogs | yes, RunPod-bound | `orchestrator/stages.py` `handle_dispatched`, `db/repository.py` `reserve_dispatch`/`confirm_dispatch`, `orchestrator/cloud.py` receipts | generalize the executor; keep the bookkeeping |
| Package verification and publication | yes | `orchestrator/cloud.py` `cloud_verifying`/`cloud_publishing`, `publish/` | unchanged |
| Service liveness heartbeat row | yes | `OrchestratorHeartbeat`, `/api/health` | pattern for node heartbeats |
| Pipeline dashboard | yes | `PipelinePanel` on `/dashboard` | add a nodes panel |

Not reused for this purpose:

- The Mac fork's native Swift player and PR #50 (authenticated finished-media
  import). Those make the Mac a *producer of finished browser packages*. This
  plan makes the Mac and Dell *executors inside the existing pipeline*, which
  keeps one delivery transform and one set of D-gates on the VPS. The
  drop-box (`docs/contributing-completed-media.md`) remains for ad-hoc
  contributions; PR #50 should be parked or closed once this lands.
- `mdc159/shizzle-worker` (retired spike) and `1215-Labs/shizzle-mac` (a
  stale copy of this repo at `3fe9d78`). Neither carries node code.


## Invariants to add (same PR as the code they guard)

- **A12 source.json is written last.** A node-downloaded source is complete
  only when `sources/<track>/source.json` exists and its sha256 matches the
  object; the orchestrator never dispatches separation on `source.mp4` alone.
- **B16 node assignments are fenced like dispatches.** An assignment is
  created only inside the lease, after `reserve_dispatch` for separation;
  a stale node cannot complete an assignment the orchestrator has failed.
- **B17 RunPod is the fallback, not the default.** With routing `prefer`,
  the orchestrator emits `fallback_runpod` only after `node_wait` has aged
  past `NODE_SEPARATE_WAIT_SECONDS`; the test asserts no RunPod dispatch
  occurs while an available node exists.
- **E6 node tokens are node-scoped.** A node token is rejected on every
  non-`/api/nodes/*` route; the public job API never returns `source_ref`.

## Phases (original plan, kept as the done criteria)

Where this section says "container", the as-built agent runs natively (see
"Node runtime: as built" and "Decisions taken"). Each phase is one or two PRs through the required `library`, `stemsplit`,
`player` and `postgres-contract` checks and the review-convergence workflow.
Each adds its tests, invariants and docs in the same PR. Production deploys
through the human-approved gate as usual.

### Phase 0: housekeeping (no deploy)

- Confirm which image the RunPod endpoint runs; the worker workflow says
  the legacy `ghcr.io/mdc159/shizzle-worker:v2` may still be pulled. Repoint
  to `ghcr.io/mdc159/shizzle/worker:sha-...` and archive
  `mdc159/shizzle-worker`.
- Decide `1215-Labs/shizzle-mac`: delete or make it a mirror. It carries no
  unique work.
- Delete the thirteen merged `fix/*`, `codex/*`, `feat/*`, `docs/currency-*`
  branches.
- Park PR #50 with a comment pointing at this document.

### Phase 1: node registry and heartbeat

Migration 0006, `nodes` and `node_assignments`, node-token auth, heartbeat
and list routes, dashboard strip, agent image that registers and beats from
the Mac mini. Inventory the containers already on the Mac and the Dell
(image, digest, output format). No routing yet. Done when the Mac shows
online on `/dashboard`, a bad token is rejected (E6 test), and the inventory
is recorded under `evidence/`.

### Phase 2: URL download through a node

`downloading` stage routing, download assignment flow, `source.json`
marker, agent runs the ingest container per assignment, player URL field.
RunPod still separates.
Done when a YouTube URL pasted into the web app plays as a published track
with the Mac doing the download and RunPod doing the split, and the job
events show `node_assigned`, `node_completed`, `runpod_dispatched`. This
phase alone makes "drop a URL" work.

### Phase 3: separation on a node, RunPod as fallback

`stemsplit/attempt.py` refactor and `--assignment` mode in the worker
image, agent runs the worker container per assignment, `dispatched` stage
routing with `NODE_SEPARATE_WAIT_SECONDS`, stall and reclaim,
`fallback_runpod` event, B16/B17 tests. Measure Mac CPU-in-container
separation and decide native MPS or container. Install the Dell agent
(CUDA in Docker Desktop/WSL2). Done when the same URL job completes with
zero RunPod calls while a node is online, and completes through RunPod when
the agents are stopped. Record both runs under `evidence/`.

### Phase 4: polish

Source-cache affinity, concurrency (one job per node today; a node may
advertise `slots`), node-side disk and image-digest checks in the
heartbeat, STS credentials per assignment, and optionally moving the delivery transform onto nodes if the
VPS's 4 vCPU ffmpeg step becomes the ceiling (next-phase draft measured it
at three to four parallel songs).

## Decisions taken 2026-11-05

Asked and answered by the owner on 2026-11-05:

| Question | Decision |
|---|---|
| Dell from day one? | **Mac mini first, Dell later.** Install order only; the code is the same. |
| RunPod `workers_max` during rollout | **Leave it as is.** `SHIZZLE_NODE_ROUTING=prefer` keeps RunPod idle while a node is online. |
| Do uploads also prefer a node? | **Yes.** Rule 3 is about load, not source type. Upload jobs go to S3 as today, then a node separates if one is online. |
| Mac fork and PR #50 | **Nodes run the existing containers; no code copy required.** Images are the interface: the production worker image for separation, the fork's ingest image for download. PR #50's import API is not the node path and stays parked. |
| Mac runtime (2026-11-05, evening) | **Native `mps`, not a container.** The plan's runtime section was container-first; the Mac inventory found no separator container and Docker Desktop cannot reach the Apple GPU, so the measurement the plan asked for was not taken and the agent runs the production handler natively. Revisit only if a GPU-capable container runtime appears on macOS. |
