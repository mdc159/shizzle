# Phase 0 risk-spike results — index

Date: 2026-08-02. Detail files: [RESULTS-0.1.md](RESULTS-0.1.md), [RESULTS-0.2.md](RESULTS-0.2.md), [RESULTS-0.3-0.4.md](RESULTS-0.3-0.4.md).

| Spike | Verdict | Headline numbers |
|---|---|---|
| 0.1 Stem-skew probe | **PASS (desktop)** · iPad + projector PENDING HUMAN RUN | Max inter-stem skew 3 ms corrected / 6 ms raw vs 50 ms bar; 14 ms max mix→video drift; one startup hard-seek, zero steady-state corrections. Media-element engine sufficient on desktop; segment-scheduler contingency not triggered. |
| 0.2 Signed-cookie media | **PASS 5/5** | 403 unauthenticated · 200 signed · 206 on both Range probes · 403 expired. OAC + signed cookies compose cleanly; Range needs zero extra config. |
| 0.3 Demucs gain/null | **PASS — policy decided** | `rescale` shifted drums −1.24 dB (poisons balance + null test). Policy: float32 stems + one common gain (this track −1.37 dB), manifest-recorded. Float null depth 22.6 dB = model ceiling. Gate thresholds proposed (profile v1): (a) ≥15 dB null, peak ≤ −6 dBFS; (b) ≥12 dB null, peak ≤ 0 dBFS. |
| 0.4 AAC bitrate | Built · **PENDING MIKE'S BLIND LISTEN** | 256k vs 320k objectively 0.13 dB apart (~19 dB null both); ALAC −129 dBFS sanity pass. **Finding: decoded AAC overshoots ~+1 dBFS at unity — master limiter/headroom is a hard requirement.** Blind set: `aac-abx/out/` + `LISTEN.md`. |

## Legacy infrastructure inventory (0.2 audit)

- **Bucket `karaoke-pimpshizzle`** — `karaoke/pub/` holds **36 complete published tracks** (7.45 GB): v3 manifests, 6 AAC stems each, video.mp4, multi-track.mp4. Directly inheritable as the new app's seed library (Phase 3 import task). `in/`+`out/`: one unpublished job (636 MB).
- **Distribution `E1TTUZICNONOHR`** (`dfpxpuycadacf.cloudfront.net`) — fronts the legacy bucket via OAC, **no trusted key groups = entire library publicly downloadable** by anyone with the domain. ⚠️ Recommend disabling once the new gated distribution serves the library (Mike's call; excluded from spike teardown).
- **RunPod endpoint `karaoke-demucs-v2`** (`3ugymq664vud7y`) — alive, reusable; workersMax=3 must be pinned to 1 in Phase 3.

## Human runs outstanding

1. **iPad Safari + Samsung projector skew runs** — `skew-probe/README.md` has the LAN steps; fills the two pending rows in RESULTS-0.1.md.
2. **Blind codec listen** — `aac-abx/LISTEN.md`; decides the AAC bitrate default.
