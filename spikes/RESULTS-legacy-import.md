# Legacy library import — results

Phase 3 work, 2026-08-02. Two deliverables: the server-side publisher module
(`server/src/shizzle_server/publish.py`) and the one-time import of the k25
legacy library into the Phase 3 track layout.

**Headline: 27 of 36 legacy folders imported and verified playable. The other 9
were not importable — their manifests reference six `.m4a` stem files that do
not exist in the bucket.** That is a finding about the legacy data, not an
import failure; details in §3.

---

## 1. Publisher module (Phase 3.4)

`server/src/shizzle_server/publish.py`, tested by
`server/tests/test_publish.py` (24 moto-backed tests, all green).

The contract, in order:

1. **Immutability guard.** If `tracks/{track_id}/{generation}/manifest.json`
   already exists the generation is complete; `publish()` returns
   `already_published=True` and copies nothing. Proved by
   `test_published_generation_is_never_overwritten`, which re-stages *different*
   bytes and confirms the published object is unchanged and zero copies ran.
2. **Verification** of every object the worker reported in `uploads[]`
   (`{file, key, sha256, size_bytes}` from `worker/s3_ops.upload_file`). Size
   from `head_object` always; sha256 per `ChecksumPolicy`:
   - `AUTO` (default) — stored `ChecksumSHA256` when S3 has one, else stream
     the object back and hash it;
   - `STORED` — require the stored checksum, fail without it;
   - `STREAM` — always hash;
   - `SIZE_ONLY` — sizes only, honestly recorded as `sha256_unverified` in the
     integrity block.
   Missing object, size mismatch, and same-length corruption all raise
   `ChecksumMismatch` (or its `StagedObjectMissing` subclass), whose `.code` is
   `ErrorCode.CHECKSUM_MISMATCH` and which converts to a non-retryable
   `StageError` via `.to_stage_error()`.
3. **Promotion** by S3 server-side copy only — `copy_object`, falling back to
   multipart `upload_part_copy` above the 5 GiB single-copy limit. Nothing is
   downloaded and re-uploaded. `test_promote_uses_server_side_copy_and_writes_manifest_last`
   asserts no `put_object`/`upload_file` call occurs during promotion.
4. **Manifest last.** The manifest is the completion marker.
   `test_manifest_absent_from_destination_until_every_object_copied` kills the
   client mid-promotion and asserts the manifest never appeared at the
   destination before that point, and that `is_published()` still returns
   `False` afterwards.
5. `publish_job()` then calls `JobRepository.publish_track`, which already
   derives a deterministic track id from the job id — so a crashed-and-rerun
   publishing stage converges on the same S3 prefix *and* the same DB row.
   `tracks.integrity` gets `{"worker": <both gates>, "publisher": <verification report>}`.

### Finding for the worker (not changed — out of lane)

`worker/s3_ops.upload_file` calls `s3.upload_file` without
`ChecksumAlgorithm="SHA256"`, so AWS stores no SHA256 for staged objects and
the publisher's default `AUTO` policy has to stream every staged object back to
the VPS to verify it. For a ~300 MiB track that is ~300 MiB of egress per
publish. Adding `ExtraArgs={"ChecksumAlgorithm": "SHA256"}` on the worker side
would let the publisher verify from `head_object` alone — the code path already
exists and is covered by
`test_verify_uses_stored_checksum_without_downloading`, which asserts no
`get_object` call happens when the stored checksum is present.

---

## 2. Legacy manifest schema diff

Full write-up: `docs/legacy-manifest-v3.md`. Highlights:

| | |
|---|---|
| Legacy prefix | `s3://karaoke-pimpshizzle/karaoke/pub/` |
| Folders | 36 |
| Objects | 295 |
| Bytes | 7,997,187,337 (7.45 GiB) |

**`default_gain: 0` is on every one of the 35 v3 legacy manifests.**
`worker/MANIFEST.md` warns the v2 `default_gain` name "must never return"; this
survey shows the field did not just have a bad name, it shipped with the value
that means *silence* under the linear reading its name implies. Whatever the k25
player did with it, it was not multiplying by it. Translation sets
`default_gain_db: 0.0` (unity, unit in the name). The one v2-era folder
(`the-pot-2d88b7a5`) carries `default_gain: 1.0`, which maps to `0.0 dB`
exactly.

Other gaps versus `worker/MANIFEST.md`:

- **`common_gain` absent** — cannot be reconstructed without re-separating.
  Omitted from imported manifests.
- **`integrity` absent** — legacy tracks predate both gates. Replaced with
  `{"source": "legacy-import", "gates": "not-run"}`, in the manifest and on
  `tracks.integrity`, so nothing can mistake an unmeasured legacy track for one
  that passed spike-0.3 thresholds.
- **`processing` absent** — replaced with
  `{"source": "legacy-import", "origin": "karaoke/pub/{folder}"}`.
- **`timeline.sample_rate_hz` is `48000`** on all 35 v3 folders; the worker
  writes the Demucs model rate (44100 for `htdemucs_6s`). Carried through
  verbatim — the importer does not decode the stems, so it cannot verify or
  correct that number. **Assumed, not verified.**
- **`multitrack` absent** even on the 26 folders that *have* `multi-track.mp4`
  on disk — the legacy manifest never referenced its own mux. Added on import.
- **`channel_offset`** (`0,2,4,…,10`, the channel-pair index into
  `multi-track.mp4`) is legacy-only and is **dropped**: TypeScript's
  excess-property check rejects unknown keys on a `Stem` literal. The value
  survives in the untouched source `stems.json`.
- **Stem id naming is already correct** — no gap. Legacy manifests already use
  `id: "shizzle"` with `file: "stems/other.m4a"`, exactly the worker's
  `STEM_ID_MAP` behaviour. Only `the-pot-2d88b7a5` has a file literally named
  `shizzle.wav`, and that is a filename, not an id.
- `merged_audio` (`stems/stems_merged.webm`) is legacy-only but the file is
  real and gets copied, so the key is preserved.

---

## 3. The nine folders that could not be imported

`scripts/import_legacy_library.py` classifies a folder as **degraded** when its
manifest references objects the bucket does not contain, and skips it by default
(`--include-degraded` overrides, emitting `stems[]` with only the surviving
files — which for these folders is *no stems at all*, a dead library entry).

Nine folders hold only `stems.json`, `video.mp4`, and
`stems/stems_merged.webm`. All six `.m4a` stems their manifests declare are
absent:

| Folder | Title | Complete twin elsewhere? |
|---|---|---|
| `1de757812a7b` | Stone Temple Pilots — Plush | yes (`d127f4701b35`) |
| `335561a490d4` | Sweet Child O' Mine | yes (`5d85aab23da8`) |
| `4838f621a4e2` | AC/DC — Let's Get It Up | yes (`6a84190d0bfe`) |
| `757be985b2f9` | AC/DC — Ride On | yes (`39d47aa2d3b2`) |
| `8fa8cb05e8be` | Temple Of The Dog — Hunger Strike | yes (`a3775eb76f96`) |
| `3ae816991eb4` | AC/DC — For Those About To Rock | **no** |
| `87eedd1c4ec2` | AC/DC — Dirty Deeds Done Dirt Cheap | **no** |
| `cdace71ff551` | AC/DC — Dirty Deeds (v3 MERGED) | **no** (same title/duration as above, also degraded) |
| `de330790cdfa` | AC/DC — Highway to Hell | **no** |

Five are redundant duplicates of tracks that did import. The other four are
genuinely lost as stem tracks — only the pre-mixed `stems_merged.webm` and the
video survive. **They are good candidates for the Phase 3 end-to-end RunPod
test**: re-splitting `video.mp4` through the new pipeline both proves the
pipeline and recovers real content.

**This is where the brief and the data disagree.** The brief said "36 folders,
each with … 6 AAC .m4a stems" and asked for 36 rows in `tracks`. The bucket does
not contain that. Importing the nine anyway would have put nine unplayable
entries in the day-one library, so the script skips them and reports them rather
than silently producing broken rows. The flag exists if that call should be
reversed.

---

## 4. Import run

```
docker compose -p shizzle -f infra/compose.yml --profile stack up -d postgres api
python scripts/import_legacy_library.py --database-url postgresql+asyncpg://…@127.0.0.1:5434/shizzle
```

| | |
|---|---|
| Folders considered | 36 |
| **Imported** | **27** (26 complete-v3 + 1 v2-era wav) |
| Skipped (degraded) | 9 |
| Failed | 0 |
| Destination objects | 268 |
| Bytes copied | 7,596,908,022 (7.08 GiB) |
| Wall clock, full run | 227.8 s |
| Effective copy rate | ~31 MiB/s (S3 server-side, same bucket, same region) |
| Slowest folder | `c14d436cf159` (Metallica Orion) — 932.6 MiB in 25.8 s |
| Fastest | `5b44629e2391` (My Michelle) — 82.4 MiB in 3.9 s |

Layout: `karaoke/pub/{folder}/…` → `tracks/{track_id}/1/…` in the **same
bucket**, `track_id = uuid5(track-ns, "import:{folder}")` via the new
`track_id_for_import()`. Media moves by S3 server-side copy through
`Publisher.copy_object` — nothing downloaded. The only object written from
scratch is the translated `manifest.json`, written **last**.

### Deviations from the brief, and why

1. **27 rows, not 36** — see §3.
2. **`channel_offset` dropped** rather than preserved — see §2. Caught by
   compiling the manifests against the real UI types; a first pass that kept the
   field produced 156 `tsc` errors.
3. **Content-Type restamped on copy.** One legacy object
   (`the-pot-2d88b7a5/video.mp4`) is stored as `binary/octet-stream`. Since
   CloudFront and `<video>` both care, the importer now sets the correct type
   per extension on copy (`Publisher.copy_object(..., content_type=…)`, which
   also threads it through the multipart path). That folder was re-imported
   after the fix — only its **destination** objects were deleted and rewritten;
   nothing under `karaoke/` was touched.
4. **`--rewrite-manifests` flag added.** Correcting the manifests for the
   `channel_offset` fix after the media was already promoted would otherwise
   have meant re-copying 7 GiB. The flag rewrites `manifest.json` in place for
   an already-published generation and leaves media alone. It is a deliberate,
   loudly-logged exception to generation immutability and is **only safe before
   a CDN fronts the prefix** — after Phase 4, cut a new generation instead.

### New code

| File | What |
|---|---|
| `server/src/shizzle_server/publish.py` | new — publisher module |
| `server/tests/test_publish.py` | new — 24 tests |
| `server/src/shizzle_server/db/repository.py` | added `track_id_for_import()` and `TrackRepository.upsert_imported()` (rows with no job behind them) |
| `scripts/import_legacy_library.py` | new — the importer |
| `docs/legacy-manifest-v3.md` | new — schema survey and diff |

---

## 5. Verification — what is proved

All of the following were run against the real bucket and the real Postgres.

| Claim | How | Result |
|---|---|---|
| `tracks` has one live row per imported folder | `select count(*) … from tracks` | **27 / 27 live**, all `integrity->>'source' = 'legacy-import'` |
| `GET /api/library` returns them | live HTTP against `shizzle-api` | **27 tracks**, titles and durations match the manifests |
| Every source object landed at the new prefix at the same size | re-listed both prefixes, compared per object | **268 objects, 0 mismatches** |
| No dangling manifest references | every `video`/`stems[].file`/`multitrack`/`merged_audio` looked up at the destination | **0 missing** |
| Legacy `stems.json` did not leak into the new layout | destination listing | **absent everywhere** |
| Legacy `default_gain` did not survive | per-stem check on all 27 manifests | **0 occurrences**; all `default_gain_db == 0.0` |
| Manifests satisfy the UI contract | all 27 compiled against the real `ui/src/types/karaoke.ts` with `tsc --strict --noEmit`, then the compiled JS run under Node with per-field assertions | **exit 0 both times; 27 manifests asserted** |
| Content types correct | `head_object` on all 268 objects | **0 wrong** |
| `karaoke/` untouched | re-listed the whole legacy prefix after the run | **295 objects / 7,997,187,337 bytes — byte-identical to the pre-run inventory** |
| Re-running the import is idempotent | ran the same two folders twice, then the full set again | second run: **0 copies, 0 bytes, rows updated in place**, no duplicates |
| Publisher unit contract | `pytest tests/test_publish.py` | **24 passed** |
| Nothing else regressed | `pytest --ignore=tests/contract` | **87 passed** |
| Lint / types | `ruff check src tests ../scripts`; `mypy src/shizzle_server/publish.py` | **clean** (two pre-existing mypy errors remain in `repository.renew_lease`, untouched) |

### Playability spot-check (2 tracks)

Every file each manifest references was fetched with a ranged presigned GET:

- **The Pot** (`f995371a…`, the v2-era wav folder) — manifest + video + 6 wav
  stems: **8/8 HTTP 206**, correct magic bytes (`RIFF` / `ftyp`), correct
  Content-Types.
- **Van Halen — (Oh) Pretty Woman** (`52eb3b91…`, full AAC folder) — manifest +
  video + 6 `.m4a` stems + `multi-track.mp4` + `stems_merged.webm`: **11/11
  HTTP 206**, correct magic bytes, correct Content-Types.

**The API cannot serve these yet, by design.** `routes._resolve_local_track_dir`
requires `tracks.s3_prefix` to start with `local/` and returns **409 Conflict**
otherwise — confirmed live for both tracks. Cloud-published tracks are meant to
go through CloudFront in Phase 4. Until then the objects are reachable only via
presigned S3 URLs (or once the CDN behaviours land). This is a **known gap, not
a regression**: `/api/library` lists all 27 correctly, but its `publicUrl`
(`/api/tracks/{id}`) will 409 until Phase 4 wires cloud serving.

### Assumed, not verified

- `timeline.sample_rate_hz = 48000` on imported tracks is carried from the
  legacy manifest and was **not** confirmed by decoding the audio.
- Audio *quality* / stem separation quality was not assessed — no integrity
  gate was run on imported material, which is exactly what
  `{"gates": "not-run"}` records.
- Actual in-browser playback was not exercised; reachability, byte-level file
  headers, and schema conformance were.

---

## 6. Environment notes for whoever runs this next

- **The compose Postgres volume was already initialized under the wrong role.**
  `shizzle_pgdata` existed from an earlier run that hit the documented
  `COMPOSE_PROJECT_NAME` / global-`POSTGRES_*` leak in `infra/compose.yml`: the
  cluster was created with role/db `gbrain`, so `shizzle` could not log in. Fixed
  non-destructively by creating the `shizzle` role and database inside the
  existing cluster — the pre-existing `gbrain` database (1 leftover job, 1
  leftover track from a Phase 2 run) was left alone. Nothing was wiped.
- Host port **5434** was used for the `stack` Postgres (`POSTGRES_HOST_PORT`)
  because a `shizzle-test` project already holds 5433.
- `AWS_ENDPOINT_URL` is set machine-wide to an R2 endpoint. Both the importer
  and the publisher tests force-clear it; anything else talking to AWS S3 from
  this box must do the same or it will silently hit the wrong provider.
- `.env` is not pure ASCII — read it with an explicit `utf-8` decode.
