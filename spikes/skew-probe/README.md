# Skew probe (spike 0.1)

Instrumented six-stem playback probe. Plays a track's muted video plus six
`<audio>` stems through one `AudioContext`, samples per-stem `currentTime`
delta vs the video every 250 ms, and reports per-stem skew, mix-to-video
drift, stall counts, and correction events.

Two modes:

- **raw** — no correction; measures how badly stems drift on their own.
- **corrected** — tiered drift policy ported from
  `k25-nextgen-rewrite/ui/src/hooks/useAudioSync.ts` (<50 ms ignore;
  50–250 ms playbackRate nudge ±0.5%; >250 ms single hard seek; stall
  detection = video advancing <0.02 s across 3 ticks at 1 s cadence).

Correction drives all stems from the *average* stem clock (as the original
hook does); instrumentation is always per-stem.

## 1. Start the server (on this PC)

```powershell
cd X:\GitHub\shizzle\spikes\skew-probe
python serve.py X:\GitHub\k25\data\47bae048e13c 8077
```

Any completed track directory works (needs `stems.json`, `video.mp4`,
`stems/*.m4a`). The server supports HTTP Range (206), required for media
seeking, and listens on all interfaces for LAN devices.

If LAN devices can't connect, open the port once (admin PowerShell):

```powershell
New-NetFirewallRule -DisplayName "skew-probe 8077" -Direction Inbound -Protocol TCP -LocalPort 8077 -Action Allow
```

Find this PC's LAN IP: `ipconfig` → IPv4 Address (e.g. `192.168.1.23`).

## 2. Run on desktop Chrome (this PC)

Open:

    http://localhost:8077/index.html?track=/track

Press **Play**. Toggle mode with the **Mode** button (toggle *before*
pressing Play for a clean single-mode run; reload the page between runs).
Watch the live table and summary; a `PROBE_SUMMARY {json}` line is logged
to the console every 5 s. Click **Download JSON report** at the end.

Automated run (used for the desktop row in `../RESULTS-0.1.md`):

```powershell
node run-probe.mjs        # needs global playwright; ~5 min, writes results\*.json
```

## 3. Run on iPad (same LAN)

1. Server running on the PC as above; firewall port open.
2. On the iPad, open Safari and go to
   `http://<pc-ip>:8077/index.html?track=/track`
   (e.g. `http://192.168.1.23:8077/index.html?track=/track`).
3. Tap **Play** (iOS requires the user gesture — the button provides it).
4. Play **at least 3 minutes in corrected mode**, note the on-screen
   summary numbers (max inter-stem skew, max mix drift, counts). Reload,
   switch to **raw**, play ~1 minute, note numbers again.
5. Tap **Download JSON report** — Safari saves to Files; AirDrop or copy
   it back to the PC into `results\` if you want the raw samples.
6. Enter the numbers in `X:\GitHub\shizzle\spikes\RESULTS-0.1.md`
   (iPad Safari row).

## 4. Run on the Samsung projector (Tizen browser)

1. Same server, same LAN.
2. Open the projector's Internet/browser app and enter
   `http://<pc-ip>:8077/index.html?track=/track`.
3. Select **Play** with the remote. Same procedure: ≥3 min corrected,
   reload, ~1 min raw.
4. The Tizen browser likely cannot download files — read the on-screen
   summary block (it holds every number the results table needs:
   max inter-stem skew, max |mix−video| drift, per-stem min/mean/max,
   nudge/hardSeek/stall counts) and photograph it or copy by hand.
5. Enter the numbers in `RESULTS-0.1.md` (Samsung projector row).

## Reading the numbers

- **delta ms** — stem currentTime minus video currentTime (positive =
  stem ahead of video).
- **max inter-stem skew** — worst spread between fastest and slowest stem
  in any single 250 ms sample. This is the number the ≤50 ms bar judges.
- **max |mix−video| drift** — worst average-stem-vs-video offset.
- **nudges / hardSeeks / stallBailouts** — correction activity in
  corrected mode (raw mode never corrects, only stall-detects).
