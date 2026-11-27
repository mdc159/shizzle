# Contributing completed media (drop-box import)

How to hand the library a **finished** `shizzle-browser-v1` package — one that
already has the six AAC stems, the silent video, and the v3 manifest — without
re-separation, re-download of any source, or re-encode. The VPS-side ingest is
issue #45; invariant C8 in [INVARIANTS](INVARIANTS.md#c8--drop-box-imports-register-only-after-validation)
governs it.

Use this only for material you produced yourself and verified plays. The
ingest re-proves every byte (sha256) and runs the full delivery audit; a
package that fails any gate is rejected and left in place.

## The drop-box

Bucket: the media bucket (`karaoke-pimpshizzle` in production). Prefix:

```text
imports/{source_ref}/
  stems/vocals.m4a  stems/drums.m4a  stems/bass.m4a
  stems/guitar.m4a  stems/piano.m4a  stems/shizzle.m4a
  video.mp4
  manifest.json      <- upload LAST; presence = drop complete
  result.json        <- written by the ingest only; never upload this
```

Nothing else may live under the prefix. Any extra object is a rejection.

`source_ref` must match `^(youtube|sha256)-[A-Za-z0-9_-]{6,128}$` — e.g.
`youtube-dQw4w9WgXcQ` or `sha256-1f0e7adbe53b`. The `youtube-`/`sha256-`
namespace keeps these ids disjoint from the legacy importer's `karaoke/pub/…`
refs; anything else is refused before any S3 call. The id determines the track
id (a deterministic uuid5), so **one source, one ref, forever**: re-dropping
the same package under the same ref converges; dropping different content
under a ref that already published is a `TRACK_CONFLICT`.

A successful ingest deletes the seven media objects from the prefix
(`manifest.json` and `result.json` remain). The ingest recognizes its own
completed work by manifest hash **before** checking the inventory, so simply
re-running the same ingest command after success reports `already-published`
— the media do not need to be re-dropped.

## Manifest requirements

The manifest is a v3 delivery manifest exactly as the lossless pipeline
writes it (`delivery_profile == "shizzle-browser-v1"`). The ingest enforces,
in order:

- `version` is exactly `3`.
- `delivery_profile` is exactly `shizzle-browser-v1`.
- `stems` lists the six canonical roles **in order** — `vocals, drums, bass,
  guitar, piano, shizzle` — each with `file == "stems/{id}.m4a"`.
- Every stem carries the **same** `default_gain_db`, a finite number `<= 0`
  (one common attenuation, never per-stem, never a boost).
- `video == "video.mp4"`.
- `duration` is finite with `0 < duration <= 1800` (the server's
  max-duration setting).
- `timeline` has **exactly** the keys `start_ms`, `duration_ms`,
  `sample_rate_hz` — no extras — with values `start_ms: 0` (the number, not a
  boolean), `duration_ms: round(duration*1000)` (finite; tolerated within
  ±1 ms), and `sample_rate_hz` 44100 or 48000 — 48 kHz is accepted only when
  every stem's measured sample rate agrees with the timeline (a mismatch is
  an `INTEGRITY_GATE_FAILED` `stem-sample-rate-mismatch`).
- `title` is a non-empty string.
- `integrity.objects` is a list declaring, for **exactly** the seven media
  files (`video.mp4` + the six stems, no more, no less, **each declared
  exactly once** — a duplicate declaration is rejected), `bytes` (positive
  integer) and `sha256` (lowercase 64-char hex).
- Each stem declares `bytes <= 64 MiB`; the video declares
  `bytes <= 128 MiB`.

Beyond the manifest, the downloaded bytes must pass the full delivery audit:
each file's real sha256 must equal the declared one, the audio must be
fast-start AAC-LC stereo (44.1 or 48 kHz — existing lossy material is
preserved, never re-encoded; a sparse stem's low bitrate is recorded as a
warning, not an error), the video must be audio-less H.264 within profile
bounds, measured stem durations must agree within 5 ms, and the complete
generation must average at most 2.5 Mb/s.

## Dropping the package

The client script (stdlib + boto3, no repo imports):

```bash
python ops/drop_browser_package.py --candidate DIR --source-ref REF \
    [--bucket karaoke-pimpshizzle] [--region us-east-1] [--wait]
```

It validates the ref and the manifest's structure (exactly the seven
canonical object declarations), verifies every local file against
`integrity.objects` **before any upload or even client creation**, uploads
the media (skipping an object only when the remote ETag equals the local
file's MD5, so an interrupted drop can be restarted by re-running while a
same-size object with different bytes is re-uploaded), and **always uploads
`manifest.json` last and unconditionally** — the completion marker never
goes through the skip path. With `--wait` it polls for the
ingest's `result.json` and prints it. Every failure prints exactly one JSON
line and exits non-zero.

The equivalent raw sequence (`aws s3 cp`), if you cannot run the script:

```bash
# media first, any order — Content-Type matters; one line per canonical stem
aws s3 cp DIR/stems/vocals.m4a  s3://karaoke-pimpshizzle/imports/REF/stems/vocals.m4a  --content-type audio/mp4
aws s3 cp DIR/stems/drums.m4a   s3://karaoke-pimpshizzle/imports/REF/stems/drums.m4a   --content-type audio/mp4
aws s3 cp DIR/stems/bass.m4a    s3://karaoke-pimpshizzle/imports/REF/stems/bass.m4a    --content-type audio/mp4
aws s3 cp DIR/stems/guitar.m4a  s3://karaoke-pimpshizzle/imports/REF/stems/guitar.m4a  --content-type audio/mp4
aws s3 cp DIR/stems/piano.m4a   s3://karaoke-pimpshizzle/imports/REF/stems/piano.m4a   --content-type audio/mp4
aws s3 cp DIR/stems/shizzle.m4a s3://karaoke-pimpshizzle/imports/REF/stems/shizzle.m4a --content-type audio/mp4
aws s3 cp DIR/video.mp4         s3://karaoke-pimpshizzle/imports/REF/video.mp4         --content-type video/mp4
# manifest LAST — its presence marks the drop complete
aws s3 cp DIR/manifest.json     s3://karaoke-pimpshizzle/imports/REF/manifest.json     --content-type application/json
```

Then run the VPS ingest (see below), or let the operator do it.

## Running the ingest (operator)

```bash
cd /opt/shizzle/prod && docker compose -f compose.prod.yml exec api \
    python -m shizzle_server.publish.browser_import --source-ref <ref>
```

Defaults come from the api container's environment (bucket, database, region,
max duration). `--dry-run` validates the whole drop and writes a
`would-publish` result (or `would-register` on a crash-recovery retry)
without copying, deleting or registering anything. Exit codes: `0`
published / already-published / would-publish / would-register, `2`
rejected, `3` not ready (no manifest yet), `4` unexpected error (sanitized
`{"status": "error", ...}` line; never raw exception text).

## result.json

The ingest writes `imports/{ref}/result.json` on every terminal outcome
except not-ready, and the CLI prints the same dict:

```json
{
  "status": "published | already-published | rejected | would-publish | would-register",
  "sourceRef": "youtube-…",
  "trackId": "uuid",
  "generation": 1,
  "s3Prefix": "tracks/{trackId}/1",
  "manifestKey": "tracks/{trackId}/1/manifest.json",
  "manifestSha256": "<sha256 of the dropped manifest bytes>",
  "code": null,
  "issues": [],
  "warnings": [],
  "at": "2026-09-07T00:00:00+00:00"
}
```

Rejection codes (`code` is non-null exactly when `status == "rejected"`):

| Code | Meaning |
|---|---|
| `MANIFEST_INVALID` | manifest.json is not valid JSON or fails any shape rule above (`issues[]` names the exact `manifest-*` code) |
| `INVENTORY_MISMATCH` | the prefix does not contain exactly the seven declared files at the declared sizes (extra, missing, or size mismatch) |
| `INTEGRITY_GATE_FAILED` | downloaded bytes fail sha256, the delivery audit, the 5 ms stem spread, the stem format/size guard, or the 2.5 Mb/s budget |
| `TRACK_CONFLICT` | this ref already published different content, or the existing row is not this drop's; generation moves are not a drop-box operation |
| `GENERATION_UNVERIFIED` | the published generation's media objects do not match the manifest's declaration (mixed or corrupted generation); nothing is registered |
| `TRACK_DELETED` | the ref's track row is soft-deleted; restore is an explicit operator action, never a drop |
| `DISK_FULL` | the ingest host lacks space for the media plus headroom |

After a rejection the dropped media, manifest, and the new `result.json`
always remain — fix the package and drop again; nothing was registered.
Staging copies or an incomplete generation prefix MAY remain after a
publisher failure until the next successful run clears them (the ingest
deletes them itself whenever it can). Nothing is registered either way.

`manifestSha256` is the sha256 of the dropped manifest bytes, on every
status — a client can correlate a receipt with exactly the manifest it
uploaded. `ops/drop_browser_package.py --wait` uses it: it returns only a
receipt whose ETag changed AND whose `manifestSha256` matches the manifest it
just uploaded, so a stale receipt from a previous drop is never returned.

On an unexpected error (S3 outage, credentials, bug) the ingest prints one
sanitized JSON line (`{"status": "error", "type": ..., "code": ...,
"message": ...}` — never raw exception text, which can carry credentials in
URLs) and exits 4. The client script sanitizes every failure the same way
and exits non-zero.

## What not to do

- Do not write under any other prefix (`tracks/…`, `karaoke/…`, `sources/…`);
  the ingest only reads `imports/{source_ref}/`.
- Do not reprocess, re-encode, or "repair" the media to satisfy a gate — fix
  the production pipeline instead. Existing lossy audio is preserved as-is.
- Do not delete or rewrite `manifest.json` once uploaded; if you must fix a
  drop, delete the whole prefix (except leave any `result.json` alone) and
  re-drop.
- Never touch another track's objects or rows. The ingest registers only the
  deterministic id for your `source_ref`, and refuses to touch anything else.

## Known limitations (accepted 2026-09-07)

- Two ingests of *different* content under the *same* `source_ref` running at
  the same instant can race between the pre-registration row check and the
  locked write; the drop-box is a single-operator command today, and the
  post-publish manifest-hash check already refuses the common case.
- `ops/drop_browser_package.py` raises a plain Python error, rather than
  printing its usual one-line JSON result, when a local candidate file cannot
  be read. The upload has not started at that point.
