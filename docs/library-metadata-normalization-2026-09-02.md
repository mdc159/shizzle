# Library metadata normalization — 2026-09-02

Operator feedback: the library's display metadata is inconsistent. Some titles
embed the artist (`Van Halen - Runnin' With The Devil`), some carry platform
junk (`(Official Music Video)`, `HD`, `4K`, remaster/resync/upscale notes),
artist casing drifts (`TOOL` vs `Tool`), and a few titles arrived with
mojibake from a bad source decode. This change rewrites
`tracks.artist` and `tracks.title` for 27 non-deleted tracks and soft-deletes
2 rows (a duplicate and a phantom; see "Deletions" below) from a reviewed
mapping. No generation, pointer, manifest, or S3 object changes — publication
immutability (INVARIANTS C1–C6) is untouched; this is a plain row update, not
a schema change (F3).

The mapping is
[`ops/data/track-metadata-2026-09-02.json`](../ops/data/track-metadata-2026-09-02.json)
(schema `shizzle-track-metadata-fix-v1`), prepared from `/api/library` on
2026-09-02 and approved by Mike. The script is
[`ops/normalize_track_metadata.py`](../ops/normalize_track_metadata.py).

## Rules applied by the mapping

1. `artist` and `title` are separate fields; the title never repeats the
   artist.
2. Drop platform junk: `(Official Music Video)`, `[Official Video]`, `HD`,
   `4K`, remaster/resync/upscale notes.
3. Keep version information that changes what you hear: live, unplugged,
   cover, guest performer, session.
4. Canonical spellings: `AC/DC`, `Guns N' Roses`, `Tool`, `Temple of the Dog`,
   `Alice in Chains`.

## Resolution and safety rules

- Entries carry either a full track `id` with an `expect {artist, title}`
  block (current values must match exactly) or an `expect_id_prefix` (8 hex
  chars, must match exactly one non-deleted track). Prefix matching exists so
  mojibake titles never have to be retyped into the mapping.
- Entries may carry `"action": "delete"` to soft-delete a duplicate instead
  of renaming it. Delete entries resolve the same way (full `id` or
  `expect_id_prefix`) and must carry an `expect` block or a `note` saying why
  the track is a duplicate. Deletion uses the same mechanism as the API
  DELETE route (`TrackRepository.soft_delete`: row lock, `deleted_at =
  utcnow()`), happens inside the same single transaction as the updates, and
  is verified on re-read (`deleted_at` set). A delete entry counts as
  covering its track. Deletions are listed separately in the before/after
  output and in the JSON report (`counts.deleted`, `deletions[]`). The media
  objects and generation history are retained; the row is only marked.
- Every non-deleted track must be covered (by an update or a delete entry);
  soft-deleted rows are ignored.
- All violations (missing id, expect mismatch, ambiguous prefix, uncovered
  track, duplicate target) are collected and printed together; any violation
  aborts with exit 2 before any write.
- Without `--apply` the script is a dry run: it prints the before/after
  tables and changes nothing.
- With `--apply`, all changes happen in ONE transaction, then every touched
  row is re-read and asserted against the target, and a JSON run record is
  written (timestamp, database host without credentials, per-track
  before/after, counts).

## Before/after table

Old values are known from the mapping's `expect` blocks where present; for
prefix-matched rows the dry run prints the exact current values (they contain
mojibake and are deliberately not retyped here). Sorted by new artist, then
new title.

| id | old artist | old title | new artist | new title | note |
|----|-----------|-----------|------------|-----------|------|
| c4072b04 | (dry run prints) | (dry run prints) | AC/DC | It's a Long Way to the Top | |
| a54803e6 | (dry run prints) | (dry run prints) | AC/DC | Let There Be Rock | |
| 4ed567d1 | (dry run prints) | (dry run prints) | AC/DC | Let's Get It Up | |
| f5b84806 | (dry run prints) | (dry run prints) | AC/DC | Ride On (live, Stade de France 2001) | |
| 9c835433 | (dry run prints) | (dry run prints) | AC/DC | Shoot to Thrill (Iron Man 2 version) | |
| e9bc18b1 | (dry run prints) | (dry run prints) | AC/DC | Walk All Over You | |
| 1edee1b7 | (dry run prints) | (dry run prints) | AC/DC | You Shook Me All Night Long | |
| c1c4ec31 | (dry run prints) | (dry run prints) | AC/DC | You Shook Me All Night Long (with Steven Tyler, 2003 Hall of Fame induction) | |
| d24e81ba | (dry run prints) | (dry run prints) | Alice in Chains | Man in the Box | |
| 1f3b9397 | (dry run prints) | (dry run prints) | Black Sabbath | Into the Void (live) | |
| fc51be9b | (dry run prints) | (dry run prints) | Guns N' Roses | Mr. Brownstone | |
| 7c8d8cf7 | (dry run prints) | (dry run prints) | Guns N' Roses | My Michelle | |
| b7a980db | (dry run prints) | (dry run prints) | Guns N' Roses | Sweet Child o' Mine | |
| 22f0177d | (dry run prints) | (dry run prints) | Metallica | Orion (live, Philadelphia 2025) | |
| ffee538e | (dry run prints) | (dry run prints) | Mother Love Bone | Stardog Champion | kept duplicate; 234a7b1c soft-deleted, see Deletions |
| 5cbd16b6 | (dry run prints) | (dry run prints, mojibake in 'Acústico') | Pearl Jam | Black (Unplugged) | source title has mojibake; matched on id prefix |
| 05ced267 | Peter Frampton | Black Hole Sun (Guitar Center Sessions) | Peter Frampton | Black Hole Sun (Guitar Center Sessions) | no change; row pinned by expect |
| 427a17cb | (dry run prints) | (dry run prints) | Skid Row | Monkey Business | |
| 99ec0110 | (dry run prints) | (dry run prints) | Soundgarden | Outshined | |
| 06ce033c | (dry run prints) | (dry run prints) | Soundgarden | Spoonman | |
| 297389b3 | (dry run prints) | (dry run prints) | Stone Temple Pilots | Plush | |
| 20d172a7 | (dry run prints) | (dry run prints) | Temple of the Dog | Hunger Strike | |
| 0244dc21 | (dry run prints) | (dry run prints, mojibake dash) | Temple of the Dog | War Pigs (Black Sabbath cover, live in San Francisco) | source title has mojibake dash; matched on id prefix |
| f995371a | TOOL | The Pot | Tool | The Pot | casing fix |
| 52eb3b91 | (dry run prints) | (dry run prints) | Van Halen | (Oh) Pretty Woman | |
| 0329507a | (dry run prints) | (dry run prints) | Van Halen | Hot for Teacher | |
| 4bd3d013 | (dry run prints) | (dry run prints) | Van Halen | You Really Got Me | |

## Deletions

Two rows are soft-deleted (`deleted_at` set, same mechanism as the API DELETE
route; media objects and generation history retained):

- **234a7b1c — Mother Love Bone, Stardog Champion.** Duplicate of ffee538e.
  Both are legacy imports; this copy is 29.97 fps / 304.5 s while the kept one
  is the official video at 30 fps / 300.7 s. Decided by Mike 2026-09-02.
- **75fae991 — (phantom row).** `s3_prefix` is
  `local/9fe2c9a570f4449abf55bcd96f7159da` with no manifest in S3; created
  2026-08-19 by job 9fe2c9a5 through a stub `test_pipeline` path that
  published in under a second. Nothing to play. Mike 2026-09-02. (Its bogus
  1.0 s duration goes away with the row; no separate duration fix needed.)

## How to run

The box receives no source code, so ship the script and mapping to the VPS and
run them inside the api image (which has `shizzle_server` and SQLAlchemy
installed and carries `DATABASE_URL` in its compose environment). From a repo
checkout:

```bash
scp ops/normalize_track_metadata.py \
    ops/data/track-metadata-2026-09-02.json \
    <vps>:/opt/shizzle/prod/ops-normalize/
```

Then on the VPS, **dry run first** and review the printed before/after table:

```bash
cd /opt/shizzle/prod
docker compose -p shizzle -f compose.prod.yml run --rm --no-deps \
  -v /opt/shizzle/prod/ops-normalize/normalize_track_metadata.py:/tmp/normalize_track_metadata.py:ro \
  -v /opt/shizzle/prod/ops-normalize/track-metadata-2026-09-02.json:/tmp/mapping.json:ro \
  api python /tmp/normalize_track_metadata.py --mapping /tmp/mapping.json
```

If the table is right and the run exits 0, apply and write the run record back
into the mounted directory:

```bash
docker compose -p shizzle -f compose.prod.yml run --rm --no-deps \
  -v /opt/shizzle/prod/ops-normalize:/tmp/ops:rw \
  api python /tmp/ops/normalize_track_metadata.py \
    --mapping /tmp/ops/track-metadata-2026-09-02.json \
    --apply --report /tmp/ops/track-metadata-2026-09-02.run.json
```

Exit 2 means a validation violation — nothing was written; read the printed
list, fix the mapping or investigate the library, and dry run again. A rerun
after a successful apply is safe: `expect` blocks then mismatch by design
(the row already holds the new values), so do not rerun blindly; treat exit 2
on a rerun as "already applied" and verify via `/api/library` instead.

## Run record

**Not yet applied.** After the production run, paste the JSON from
`ops/data/track-metadata-2026-09-02.run.json` (timestamp, database host,
counts, per-track before/after) here, and note the operator and the dry-run
review that preceded it.
