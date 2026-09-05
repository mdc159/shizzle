# Testing and playback validation

This is the current test entry point. Run the checks for the area changed, then
use the live playback procedures when a media, delivery, or playback change
needs direct evidence. Existing experiment records remain useful for explaining
codec and synchronization decisions; their old completion checklists are not
new setup requirements.

## Local automated checks

Run from the repository root in PowerShell. Python environments are managed by
`uv`; the player uses the committed npm lockfile and Node 22 in CI.

```powershell
uv sync --directory library
uv run --directory library pytest -q -m "not postgres"
uv run --directory library pytest ../ops/tests/test_normalize_track_metadata.py -q
uv run --directory library ruff check .
uv run --directory library mypy src

uv sync --directory stemsplit
uv run --directory stemsplit pytest -q
uv run --directory stemsplit ruff check .

Push-Location player
npm ci
npm run build
npm run lint:all
npx playwright install chromium
$env:SHIZZLE_E2E_HEADLESS = '1'
npx playwright test e2e/library-scroll.spec.ts e2e/remote-mixer.spec.ts e2e/source-title-parity.spec.ts e2e/upload-metadata.spec.ts --reporter=list
Pop-Location
```

The four listed browser specs use fixtures or mocked API/WebSocket responses;
they do not submit a real separation job. Clear `SHIZZLE_E2E_BASE_URL` in this
shell before running them so Playwright starts local Vite. Vite defaults its API
proxy to `http://localhost:8001`; `SHIZZLE_API_PROXY` overrides it.

`npm run build` includes TypeScript checking. `lint:all` runs ESLint and Knip;
Knip can print unused-export warnings without failing under the current
`knip.json`. The library mypy step is currently informational in CI. The
required `player` CI job runs build, ESLint, and **only**
`e2e/library-scroll.spec.ts`, so green CI does not imply every retained browser
spec has passed. The source of truth for check selection is
[ci.yml](../.github/workflows/ci.yml).

The retained `e2e/stem-split.spec.ts` is an old full upload/separate/play
experiment with a machine-specific MP4 path and stale UI/video expectations.
It is not a ready-to-run smoke test: it lacks current authentication and artist
entry, expects the old “Extracting Audio” label, and expects a direct video URL
instead of the staged Blob. Do not use an unqualified `npx playwright test`
as the default validation command until this specimen is updated.

## PostgreSQL and deployment contracts

Use a disposable PostgreSQL 16 database. The tests apply the real Alembic
migrations and exercise leases, concurrent claims, crash/restart, and publish
transactions. Never point this suite at production or a database to preserve.

```powershell
$env:SHIZZLE_TEST_DATABASE_URL = 'postgresql+asyncpg://shizzle:shizzle@127.0.0.1:5433/shizzle'
uv run --directory library pytest -q -m postgres
```

CI also checks `alembic downgrade -1` followed by `alembic upgrade head` against
its disposable database, with `DATABASE_URL` set to that database. Transaction
and RunPod endpoint failure injection are Bash tests, run in CI or a Linux
checkout:

```bash
bash deploy/vps/tests/test_release_transaction.sh
bash deploy/runpod/tests/test_repoint.sh
```

See [AUTOMATION.md](AUTOMATION.md) for what deploy/rollback scripts do and
[INVARIANTS.md](INVARIANTS.md) for the contracts these suites guard.

## Live browser playback: continuous play and scrubbing

Use `https://shizzle.systems` for production playback evidence. The browser is
a consumer of the cloud application; media is supplied by the deployed stack.
Start with the affected track or a representative small set, including a long
track and a quiet intro if those are relevant. A full-library run is a deliberate
expansion, not required for every documentation or UI change.

1. Record application build, browser/version, time, track id, active generation,
   and `delivery_profile`. Sign in through the normal passcode gate.
2. Load a track and wait for Play to enable. Confirm video `currentSrc` is
   `blob:` and `data-staged-bytes` is positive. Six AAC stems stream separately;
   the audio-less master video is staged before playback, with a 128 MiB cap.
3. Click Play and inspect `window.__shizzlePlaybackHealth.getMetrics()`. Require
   advancing video and all six stems, no media error, running AudioContext,
   sensible per-stem and master PCM, and settled synchronization. Listen as
   well: moving clocks and nonzero PCM do not prove audible quality.
4. Seek back and forth across early, middle, and late sections. Include adjacent
   targets, large jumps, and repeated dragging while playing. Record each target,
   time to a fresh healthy observation, stem/video offsets, recovery incidents,
   and whether sound resumes without chirps, duplication, or persistent gaps.
5. Pause/resume repeatedly; exercise vocals mute, each solo, faders, Reset, and
   master volume. Include zero volume followed by restoration. Test the Space
   shortcut separately from the Play button; these are different UI paths.
6. Let a track reach its natural end, replay it, and verify the opening seconds
   start together. Repeat when investigating end/replay regressions.
7. Switch tracks in the same page. Verify old media/nodes stop, the previous
   video Blob is revoked, and recovery/incident state belongs to the new track.
   Stem gains, mute/solo, and master volume are persisted preferences in the
   current store: track selection does not automatically reset them. Use Reset
   mixer for unity/unmuted/unsoloed stems; restore master volume separately.

The retained acceptance bounds are recovery/seek settlement within 3 seconds,
maximum settled inter-stem spread at most 50 ms, and maximum absolute
stem-to-video offset at most 50 ms. The automated `waitHealthy` helper is
stricter at 40 ms and requires non-null master PCM, so diagnose real source
silence explicitly instead of treating that helper alone as an audio verdict.
Initial Play has a separate 15-second startup allowance in the harness.

## Reproducible live automation

The existing harness is
[production-playback.spec.ts](../player/e2e/playback/production-playback.spec.ts).
Set the passcode through the test process environment; do not put it in a
tracked script, report, command history, or captured network log. With
`SHIZZLE_E2E_PASSCODE` already available, run from `player`:

```powershell
$env:SHIZZLE_E2E_BASE_URL = 'https://shizzle.systems'
$env:SHIZZLE_PRODUCTION_PLAYBACK = '1'
$env:SHIZZLE_E2E_HEADLESS = '1'
$env:SHIZZLE_TRACK_ID = '<track id from the authenticated library>'
$env:SHIZZLE_PLAYBACK_MODE = 'stress'
$env:SHIZZLE_SEEK_SEED = '1511464998'
$env:SHIZZLE_RESULT_PATH = '../output/playwright/playback-stress.json'
npx playwright test e2e/playback/production-playback.spec.ts --workers=1 --reporter=list
```

| Mode/control | Current behavior |
|---|---|
| `stress` (default) | 20 deterministic seeks within 3–97% of duration; 12 rapid button pause/resume cycles; mute/solo/reset; stopped-stem recovery. |
| `natural` or `playlist` | Full playthrough followed by replay; both names currently use the same implementation. `SHIZZLE_REPEAT_COUNT` controls completed playthroughs per track. |
| `faults` | Abort a real media Range request, freeze/restore the page for 750 ms, and seek with 150 ms latency, 4 Mb/s down, 1 Mb/s up. Uses Chromium CDP. |
| `SHIZZLE_TRACK_ID` | Select one exact id. Without it, the harness selects the whole returned library. |
| `SHIZZLE_TRACK_LIMIT` | Optional positive limit applied after track selection. |
| `SHIZZLE_SEEK_SEED` | Repeat the same pseudo-random seek sequence; included in the JSON report. |
| `SHIZZLE_RESULT_PATH` | Write incremental results and final metrics; use a different filename for each mode/run. |
| `SHIZZLE_DISABLE_QUIC=1` | Optional diagnostic Chromium flag, not the baseline networking configuration. |

The companion `e2e/playback/video-staging-cap.spec.ts` mocks an oversized video
response while using an authenticated library/stem path. It checks rejection of
a declared size above the cap, not a complete streaming-memory bound.

`e2e/remote-mixer-live.spec.ts` exercises a real local relay when
`SHIZZLE_E2E_LIVE_RELAY=1` and an auth-off control plane is available through
`SHIZZLE_API_PROXY`. `e2e/remote-mixer-vm.spec.ts` uses two authenticated browser
contexts against `SHIZZLE_E2E_BASE_URL` with `SHIZZLE_E2E_PASSCODE`; it checks
remote mute against measured player PCM and currently selects a track matching
“Black Hole Sun”. These live tests have side effects in
their test browser sessions and must be scoped to the intended environment.

Never retain tokens, cookies, passcodes, full signed query strings, or raw
credential-bearing manifests. Capture redacted artifact paths, build/generation,
numeric metrics, and the exact failure instead. See invariant E1.

## Stem optimization and listening experiments

The current contracts are float32 lossless worker stems at 44.1 kHz, six
canonical roles, and one common attenuation recorded as `default_gain_db` in
the delivery manifest for playback. New browser stems use AAC-LC 256 kb/s at
44.1 kHz. The player applies
fixed -3 dB master headroom and a compressor/limiter. These are implemented
choices; earlier exploratory thresholds do not override
[delivery_profile.py](../library/src/shizzle_server/publish/delivery_profile.py)
or invariants A2–A3 and D1–D7.

Preserve and reuse these procedures when evaluating a proposed change:

1. Compare float-preserving separation with per-stem rescale using the same
   source decode, model settings, and sample timeline. Measure each stem and
   unity remix; record common gain, sample count, alignment, residual RMS/peak,
   and reconstruction null depth. Per-stem rescaling changes balance.
2. Encode identical gained stems as AAC 256k, AAC 320k, and ALAC, then decode and
   sum each set. Compare alignment, decoded lengths, residuals, and true peak
   against the float remix. AAC can overshoot after decoding; measure it.
3. For listening, use lossless remix files, apply identical trim to every
   rendition, randomize labels, and keep the answer key closed. On the actual
   listening chain, compare dense choruses, cymbal decay, vocal sibilance, and
   quiet endings at matching timestamps. Write distinguishability and ranking
   before unblinding. Objective residuals are diagnostic, not listener scores.
4. For an affected production track, listen to default mix, vocals muted, every
   solo role, loud passages, seeks, and transitions. Record timestamps for
   clipping, pumping, missing content, phase artifacts, gaps, and gain jumps in
   the [listening worksheet](../evidence/cloud-continuous-playback/listening-worksheet.md).

Retained references:

- [Gain and codec results](../evidence/spikes/RESULTS-0.3-0.4.md),
  [separation experiment script](../evidence/spikes/demucs-gain/run.py),
  [rendition script](../evidence/spikes/aac-abx/make_renditions.py), and
  [blind-listen instructions](../evidence/spikes/aac-abx/LISTEN.md). The old
  scripts reference a previous local source/Docker setup; supply a suitable
  source and environment before rerunning. The recorded pending blind verdict
  is not evidence of a completed human listen.
- [Decoder-clock experiment](../evidence/spikes/RESULTS-0.1.md) and
  [Range/auth experiment](../evidence/spikes/RESULTS-0.2.md).
- [Playback findings and repairs](../evidence/cloud-continuous-playback/evidence.md),
  [accepted snapshot](../evidence/cloud-continuous-playback/acceptance-matrix.md),
  and [browser capability contract](../evidence/cloud-continuous-playback/browser-conformance.md).
  Their 27-track figures describe the recorded acceptance snapshot, not a live
  inventory or a fresh test of the current deployment.

For symptom-specific investigation, use
[playback-troubleshooting.md](playback-troubleshooting.md).
