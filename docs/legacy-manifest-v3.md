# Legacy library manifest schema (`karaoke/pub/*/stems.json`)

What is actually in `s3://karaoke-pimpshizzle/karaoke/pub/`, and how it differs
from the manifest the new GPU worker writes (`worker/MANIFEST.md`).

Surveyed from the retained source set by reading all 32 manifests and listing every object.
Source of truth for the translation performed by
`scripts/import_legacy_library.py`.

## 1. What is in the legacy bucket

| | |
|---|---|
| Prefix | `karaoke/pub/{job}/` |
| Folders | 32 |
| Objects | 283 |
| Bytes | 7,822,113,584 (7.29 GiB) |

Three distinct folder shapes:

| Shape | Folders | Objects each | Contents |
|---|---|---|---|
| **complete-v3** | 26 | 10 | `stems.json`, `video.mp4`, `multi-track.mp4`, `stems/{vocals,drums,bass,guitar,piano,other}.m4a`, `stems/stems_merged.webm` |
| **merged-only** | 5 | 3 | `stems.json`, `video.mp4`, `stems/stems_merged.webm` — **the six `.m4a` stems the manifest references do not exist** |
| **v2-era wav** | 1 (`the-pot-2d88b7a5`) | 8 | `stems.json` (no `version` key), `video.mp4`, `stems/{vocals,drums,bass,guitar,piano,shizzle}.wav` |

The five **merged-only** folders are broken as stem tracks: their `stems.json`
declares `stems/*.m4a` files that were never uploaded (or were deleted). Only
the pre-mixed `stems_merged.webm` and the video survive. Every retained
merged-only folder duplicates a complete-v3 folder with the same title and
duration and is excluded from the library.

Affected folders: `1de757812a7b`, `335561a490d4`, `4838f621a4e2`,
`757be985b2f9`, `8fa8cb05e8be`.

## 2. Legacy schema

31 of 32 manifests carry exactly these top-level keys:

```
version, title, artist, duration, video, timeline, video_meta, stems, merged_audio
```

`the-pot-2d88b7a5` is older and carries:

```
title, artist, duration, video, sourceUrl, videoId, stems
```

A representative complete-v3 stem entry:

```json
{ "id": "vocals", "name": "Vocals", "file": "stems/vocals.m4a",
  "default_gain": 0, "channel_offset": 0 }
```

and the v2-era one:

```json
{ "id": "vocals", "name": "vocals", "file": "stems/vocals.wav",
  "default_gain": 1.0 }
```

## 3. Diff against the worker v3 manifest

| Field | Legacy | Worker v3 (`worker/MANIFEST.md`) | Import action |
|---|---|---|---|
| `version` | `3` (31), absent (1) | `3` | force `3` |
| `title` | present, real | present | preserved verbatim |
| `artist` | `""` on all 31 v3 folders; `"TOOL"` on the v2-era one | present | preserved verbatim (mostly empty) |
| `duration` | real ffprobe seconds | same | preserved verbatim |
| `video` | `"video.mp4"` | `"video.mp4"` | unchanged |
| `timeline.sample_rate_hz` | `48000` on all 31 | **stem/model rate** (44100 for `htdemucs_6s`) | preserved verbatim; see §4 |
| `timeline.start_ms` / `duration_ms` | present | present | preserved verbatim |
| `video_meta` | present (31), absent (1) | present | preserved; `{}` when absent |
| `stems[].id` | already UI `StemId` incl. `shizzle` | same | unchanged |
| `stems[].name` | `"Vocals"` (31) / `"vocals"` (1) | `"Vocals"` | title-cased for the v2-era one |
| `stems[].file` | `stems/other.m4a` for id `shizzle` (31); `stems/shizzle.wav` (1) | `stems/other.m4a` | unchanged (files are copied under their existing names) |
| **`stems[].default_gain`** | **`0` (31) / `1.0` (1), linear** | — removed — | **dropped** |
| **`stems[].default_gain_db`** | — absent — | dB, `0.0` | **added, `0.0`** |
| `stems[].channel_offset` | `0,2,4,6,8,10` — channel-pair index into `multi-track.mp4` | — absent — | **dropped** (see §6) |
| `merged_audio` | `"stems/stems_merged.webm"` | — absent — | preserved when the file exists (additive) |
| `multitrack` | — absent, though `multi-track.mp4` is present on 26 folders — | `"multi-track.mp4"` | **added when the file exists** |
| `common_gain` | — absent — | `{linear, db, max_float_stem_peak, max_unity_sum_peak}` | **cannot be reconstructed**; omitted |
| `integrity` | — absent — | `{profile_version, gate_a, gate_b}` | replaced with an import marker (§5) |
| `processing` | — absent — | `{model, segment, overlap, shifts, stem_codec, stem_bitrate, float_preserved}` | replaced with `{"source": "legacy-import"}` |
| `sourceUrl`, `videoId` | absent (31), `""` (1) | present, may be empty | emitted as `""` |

### The `default_gain: 0` bug

`worker/MANIFEST.md` warns that the v2 `default_gain` field "must never
return". This survey shows the bug is worse than a naming problem: **every one
of the 31 retained legacy v3 manifests carries `default_gain: 0`.** Read as the linear
gain the field name implies, that is silence on all six stems. Whatever k25's
player did with the value, it was not multiplying by it. `default_gain_db: 0.0`
carries its unit in the name and means unity, which is what the tracks actually
want; the v2-era `default_gain: 1.0` also maps to `0.0 dB` exactly.

### Stem id naming — no gap

The "`other` vs `shizzle`" mapping is already applied in the legacy manifests:
`id` is `shizzle` while `file` is `stems/other.m4a`. That matches the worker's
`STEM_ID_MAP` behaviour exactly, so no id rewriting is needed. The v2-era
folder is the only one whose file is literally named `shizzle.wav`, and that is
a filename, not an id.

## 4. `timeline.sample_rate_hz` — preserved, not corrected

Legacy declares `48000` on all 31 retained v3 folders; the new worker writes the Demucs
model rate (`44100` for `htdemucs_6s`). The importer does **not** rewrite this:
it does not decode the legacy `.m4a` files, so it cannot know their true rate,
and inventing a number would be worse than carrying the one the producing
pipeline recorded. Anything that depends on the stem sample rate must probe the
audio, not trust this field, for imported tracks. Marked as untested.

## 5. Integrity block for imported tracks

Legacy tracks predate both integrity gates. The importer writes, in the
manifest and on `tracks.integrity`:

```json
{ "source": "legacy-import", "gates": "not-run" }
```

so nothing downstream can mistake an unmeasured legacy track for one that
passed the spike-0.3 thresholds.

## 6. Fields the UI actually needs, and where the UI type is short

`ui/src/types/karaoke.ts` requires `title`, `artist`, `duration`, `video`, and
`stems[]` of `{id, name, file, default_gain_db}`; `version`, `timeline`,
`video_meta`, `sourceUrl`, `videoId` are optional. The translated manifest
supplies all of those for every complete folder — verified by compiling all 27
imported manifests against the real `.ts` file with `tsc --strict` and then
running the assertions under Node.

Two things that survey turned up:

1. **`channel_offset` is dropped.** TypeScript's excess-property check rejects
   unknown keys on a `Stem` object literal, so keeping it would make the
   manifests fail a strict literal assignment. It is legacy-only, absent from
   the worker schema, and the value survives untouched in the source
   `karaoke/pub/*/stems.json`, so nothing is lost.

2. **`StemsManifest` is a subset of the worker schema.** It has no
   `multitrack`, `common_gain`, `integrity`, or `processing` — all four of
   which `worker/MANIFEST.md` defines and the worker writes. That is a gap in
   the UI types, not in the manifests: at runtime the player gets the manifest
   from `res.json()` (typed `any`/`unknown`), so extra keys pass through
   harmlessly. Worth closing when the player starts reading integrity or
   `multi-track.mp4`.
