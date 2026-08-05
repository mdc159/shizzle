# Spike 0.1 results — six-stem skew probe

> Troubleshooting experiment. Device-specific pending rows below are not current
> acceptance work. Use this procedure only when investigating a real timing or
> synchronization issue; the finished system uses one capability-based browser
> contract.

Probe: `spikes/skew-probe/` (see its README for the original experiment).
Track: `X:\GitHub\k25\data\47bae048e13c` — "acdc-flick", 199.7 s, six AAC
`.m4a` stems + muted `video.mp4`, served locally with Range (206) support.

Threshold under test (design spec §5): stems must hold **≤50 ms** skew for
the media-element engine to be viable; otherwise the aligned segment
scheduler fallback is required.

Metrics: **max inter-stem skew** = worst (max stem delta − min stem delta)
in any 250 ms sample; **max mix→video drift** = worst |average stem clock −
video clock|. "Steady-state" excludes the first 3 s (cold-cache startup
transient, corrected by the policy's own single hard seek at t≈1 s).

## Results

| Device | Mode | Duration | Max inter-stem skew | Max mix→video drift (steady-state) | Nudges | Hard seeks | Stall bailouts | Stem waiting/stalled events | Verdict vs 50 ms |
|---|---|---|---|---|---|---|---|---|---|
| Desktop Chrome 150 (this PC, Win 11) | corrected | 190 s | **3 ms** | 14 ms (537 ms startup transient before first correction) | 0 | 1 (startup) | 0 | 6 / 0 (all at load) | **PASS** |
| Desktop Chrome 150 (this PC, Win 11) | raw | 65 s | **6 ms** | 5 ms | — | — | 0 | 0 / 0 | **PASS** (no correction needed) |
| iPad Safari | corrected + raw | ≥3 min + ~1 min | | | | | | | PENDING HUMAN RUN |
| Samsung projector (Tizen browser) | corrected + raw | ≥3 min + ~1 min | | | | | | | PENDING HUMAN RUN |

Per-stem steady-state delta vs video, corrected run (min / mean / max ms):
all six stems identical at −7 / +3.2 / +14 — stems move as one block;
skew between stems never exceeded 3 ms across 760 samples.

## Desktop Chrome run details

- Automated via Playwright (`run-probe.mjs`, channel `chrome`, headed,
  `--autoplay-policy=no-user-gesture-required --mute-audio`; `--mute-audio`
  silences output only — media clocks and sync behavior are unaffected).
- Corrected run: 190 s of the 199.7 s song (≥3 min requirement met). The
  only correction the tiered policy ever fired was the single hard seek at
  t=1.0 s recovering the cold-cache startup gap (stems briefly −537 ms vs
  video while first buffering; 6 `waiting` events, all during load). After
  that: zero nudges, zero seeks, drift never left the <50 ms ignore band.
- Raw run (warm cache): no startup transient, no corrections possible, and
  drift still never exceeded 7 ms over 65 s.

**Reading:** on desktop Chrome the six media elements natively hold sync
one order of magnitude inside the 50 ms bar; the drift policy's job here is
only startup alignment and stall recovery. The decisive tests are the two
pending constrained devices — the fallback decision (aligned segment
scheduler) waits on those rows.

Raw reports (full 250 ms sample streams + correction logs):
`spikes/skew-probe/results/skew-report-corrected.json`,
`spikes/skew-probe/results/skew-report-raw.json`,
`spikes/skew-probe/results/summaries.json`.

## How to fill the pending rows

Run per `spikes/skew-probe/README.md` §3 (iPad) / §4 (projector): serve
from this PC, open `http://<pc-ip>:8077/index.html?track=/track`, play
≥3 min corrected, reload, ~1 min raw, copy the on-screen summary numbers
into the table above (the summary block shows every column).
