# Re-split of the four lost AC/DC tracks — results

Phase 3 work, 2026-08-02. Deliverable: a reusable "existing source → stems in
the library" driver (`scripts/resplit_track.py`) and its use to recover the
four AC/DC tracks that `spikes/RESULTS-legacy-import.md` §3 flagged as
genuinely lost — their legacy folders kept a manifest but the six stem `.m4a`
files were gone from S3, and no complete twin exists elsewhere.

**Headline: the script is built, verified end-to-end short of the GPU step, and
all four jobs are submitted with recorded job ids. The RunPod endpoint went
fully unhealthy mid-run (it is under separate validation, as warned), so the
four jobs are parked `IN_QUEUE` and the tracks rows are pending. The run is
idempotent and resumable — one command finishes it once a worker is healthy.**

---

## 1. What the script does (`scripts/resplit_track.py`)

A reusable driver, modelled on `scripts/import_legacy_library.py` and reusing
the *exact* server publish/repository code, not a parallel re-implementation.

1. **Stage** a worker-ready `source.mp4` at
   `tracks/{track_id}/1/staging/source.mp4`. Three source modes:
   - `legacy-recover` (the AC/DC path) — see §2;
   - `s3-copy` — an existing S3 object that already has an audio stream is
     promoted by server-side copy;
   - `local-upload` — a local file is uploaded.
   Track ids are deterministic: `track_id_for_import(folder)` — the *same*
   namespace the legacy importer uses, so a re-split converges on the canonical
   id for that folder instead of minting a stray one.
2. **Submit** a RunPod serverless job to `RUNPOD_ENDPOINT_ID`
   (`POST /v2/{id}/run`) with the worker's documented input schema
   (`input_key`, `output_prefix`, `bucket`, `metadata`, `model`,
   `create_multitrack_mp4`), then **poll** `GET /v2/{id}/status/{job}` to a
   terminal state with exponential backoff (5 s → 30 s cap), a generous default
   timeout (`--poll-timeout`, 1800 s), tolerance of transient HTTP errors, and a
   logged line on every status transition. Cold start + weights download is
   expected to take minutes.
3. **Publish** on `COMPLETED` with `shizzle_server.publish.Publisher.publish()`
   — the server module — which verifies the six staged stems + video + manifest
   against the worker's reported sizes/sha256 (default `AUTO` policy streams
   each object back since the worker stores no S3 checksum), then promotes
   staging → `tracks/{track_id}/1/` by server-side copy, **manifest last**. It
   then re-lists the destination and asserts `manifest.json` + `video.mp4` + ≥6
   `stems/*.m4a` are present, and writes the `tracks` row via
   `TrackRepository.upsert_imported(...)` — the same repository method the
   importer uses — stamping `integrity` with `{source: resplit-runpod,
   legacy_folder, runpod_job_id, worker: <both gates>, publisher: <report>}`.

**Idempotent & resumable, proven (not just claimed):**
- A staged source that already exists is reused — no re-mux, no re-upload.
- A RunPod job id already recorded in the state file is re-polled, not
  resubmitted (`--force-resubmit` overrides).
- An already-published generation (`manifest.json` present at the destination)
  is a no-op that still reconciles the DB row.
- Per-track state (`spikes/resplit-state.json`) is written after every step, so
  an interrupted run resumes from where it stopped.

**Phasing.** With N tracks the driver stages+submits *all* jobs first (phase A),
persisting every job id, then polls+publishes each (phase B) — so all jobs run
concurrently on the endpoint's workers and no job id is lost to a long poll.

**Other flags:** `--dry-run`, `--all-lost`, `--folder`, `--local-file`,
`--source-key`, `--bucket`, `--database-url` / `--db-port`, `--no-db`,
`--checksum-policy`, `--no-multitrack`, `--report`.

Lint clean (`ruff check`).

---

## 2. The finding that shaped the recovery

The brief said to re-split each folder's `video.mp4`. Probing the real objects
first changed the approach — two facts the brief did not have:

1. **The destination bucket is `karaoke-pimpshizzle`, not `shizzle-media`.**
   `S3_MEDIA_BUCKET=shizzle-media` does not exist yet (`NoSuchBucket`); the
   legacy import wrote `tracks/` into `karaoke-pimpshizzle` and the 27 imported
   generations live there. Re-splits land beside them in the same bucket. The
   bucket is passed explicitly in the RunPod job input, so the run does not
   depend on the endpoint's own `AWS_S3_BUCKET`.

2. **The legacy `video.mp4` is video-only (h264, no audio).** Re-splitting it
   directly would separate *silence*. The audio survives only in
   `stems/stems_merged.webm`, and that file is **not a stereo pre-mix — it is
   12-channel opus**: the six stems packed as consecutive stereo channel pairs
   (exactly the legacy `channel_offset` 0,2,4,6,8,10). Demucs stems sum to their
   input, so the recovery reconstructs the original stereo mix by summing every
   left channel and every right channel
   (`pan=stereo|c0=c0+c2+…+c10|c1=c1+c3+…+c11`), muxes that with the surviving
   video (video stream copied, audio encoded AAC 320k), and re-separates the
   result. This opus→aac→Demucs path is mildly lossy but recovers content that
   was otherwise gone. The first mux attempt failed loudly
   (`aac … Unsupported channel layout "12 channels"`) — which is how the
   12-channel structure was discovered — and the fix is adaptive to any even
   channel count, so ordinary stereo sources pass straight through.

---

## 3. The four tracks, job ids, and outcomes

Bucket `karaoke-pimpshizzle`, generation 1, model `htdemucs_6s`, source built by
video+merged-mix mux (§2).

| Folder | Title (artist AC/DC) | ~dur | track_id | RunPod job id | Outcome |
|---|---|---|---|---|---|
| `3ae816991eb4` | For Those About To Rock | 378.6 s | `7889f2e0-42ed-5c1b-9792-356542a739ff` | `462bead7-b068-4dad-8b58-05c03420d8f6-u1` | **queued** |
| `87eedd1c4ec2` | Dirty Deeds Done Dirt Cheap | 282.0 s | `72756d31-83ec-544b-bcc5-7559964dc403` | `977e4f1d-96ca-4aaf-9420-5b2357939408-u2` | **queued** |
| `cdace71ff551` | Dirty Deeds (v3 MERGED) | 282.0 s | `54752361-81c8-5c33-a63d-b7d0d35dae76` | `0558f4ce-601a-4b5b-b7dd-37ddfcb88dc6-u2` | **queued** |
| `de330790cdfa` | Highway to Hell | 207.1 s | `8273cbb1-7ce0-5b7e-bc96-d9f2b1c2a2ed` | `95300bab-e1e1-4315-a478-c6a6c231804d-u1` | **queued** |

"queued" = the job was accepted by RunPod (`POST /run` → 200, `IN_QUEUE`) and is
waiting server-side; no worker has processed it yet. None `COMPLETED`, none
`FAILED`.

### Why queued, not completed

At submit time the endpoint reported `workers: ready=3`. Within a minute of the
first submission it flipped to `workers: {unhealthy: 3, ready:0, running:0}`
with `jobs.inQueue=5` (the four re-split jobs + the one proof job that was
validating the endpoint), and it held that state for the entire ~15-minute
observation window — no worker ever picked a job up. This is the "endpoint may
still be flaky, it is being validated" condition the brief anticipated. Per the
instruction, the jobs were submitted, their ids recorded, and the run made
resumable rather than blocked on a dead queue. The endpoint template/config was
not touched (jobs were only submitted and polled).

---

## 4. S3 output verification

| Claim | How | Result |
|---|---|---|
| Four sources staged | `head_object` on each `tracks/{id}/1/staging/source.mp4` | **4/4 present** — 61.2 / 48.4 / 48.4 / 41.1 MiB (the two "Dirty Deeds" twins mux to the identical size, consistent with their identical legacy duration) |
| Each source is a real re-splittable file | `ffprobe` on the muxed output | **h264 video + stereo AAC audio** (reconstructed from the 12-channel mix) |
| No stems promoted yet | list `tracks/{id}/1/` | only `staging/source.mp4` under each — **no `manifest.json`, no `stems/`** (worker has not run), so nothing is half-published |
| Legacy source untouched | read-only `GET`/`HEAD` only on `karaoke/pub/…` | the recovery never writes under `karaoke/pub/` |

The worker's own output (6 stems + `video.mp4` + `manifest.json` + optional
`multi-track.mp4`) will appear under `…/staging/` when a job runs, and the
publisher's verify+promote+re-list gate must pass before any `tracks` row is
written. That gate is wired and unit-tested (`server/tests/test_publish.py`);
it simply has not had a worker output to act on yet.

---

## 5. DB rows

**Pending — none written yet, by design.** A `tracks` row is written only after
the publisher verifies and promotes real stems, so with the jobs still queued no
row was created. The DB path itself is proven live: the driver connected to the
stack Postgres (`127.0.0.1:5434`, the exposed port — `.env`'s `DATABASE_URL`
points at the compose-internal host `postgres`, unreachable from the box) and
logged `DB connected: rows will be written`. When the jobs complete, the row is
written by `TrackRepository.upsert_imported`. If the DB were down at that moment
the S3 promotion still completes and the row is recorded as
`pending-db-unreachable` rather than failing the recovery.

---

## 6. How to finish it (resume)

Once the endpoint has a healthy worker (`GET /v2/{id}/health` shows
`workers.ready ≥ 1`):

```
uv run --directory server python ../scripts/resplit_track.py --all-lost \
    --report ../spikes/resplit-report.json
```

This reuses the staged sources and the recorded job ids
(`spikes/resplit-state.json`), polls them to `COMPLETED`, publishes, and writes
the four rows. RunPod retains async results only ~30 min after a job finishes;
if a job has aged out or the queue was cleared by the endpoint validation,
re-run with `--force-resubmit` to submit fresh jobs against the same
(already-staged) sources.

Verified idempotent on the second pass: it logged
`staged source already present, reusing` and `resuming existing runpod job …`
for all four — no re-mux, no re-upload, no resubmit.

---

## 7. Deviations from the brief, and why

1. **Source is a reconstructed mix, not `video.mp4`.** `video.mp4` is
   video-only; the audio lives in the 12-channel `stems_merged.webm`. Documented
   in §2.
2. **Destination bucket `karaoke-pimpshizzle`, not `shizzle-media`.** The latter
   does not exist; the former holds the rest of the library. §2.
3. **DB DSN defaults to `127.0.0.1:5434`**, not `.env`'s `DATABASE_URL` (which
   names the compose-internal host `postgres`). Overridable with
   `--database-url`.
4. **Outcomes are "queued", not "completed".** The endpoint went unhealthy
   under its concurrent validation. Jobs submitted + recorded + resumable, as the
   brief directed for exactly this case. §3.

---

## 8. Files

| File | What |
|---|---|
| `scripts/resplit_track.py` | new — the reusable re-split driver |
| `spikes/resplit-state.json` | per-track resume state (track ids + RunPod job ids) |
| `spikes/resplit-report.json` | machine-readable run report |
| `spikes/RESULTS-resplit.md` | this document |
