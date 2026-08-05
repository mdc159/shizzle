# Playback and infrastructure experiment index

These experiments are retained for troubleshooting. They are not unfinished
requirements and should not be rerun as a default library campaign.

Current project status and next work are in `../README.md` and
`../docs/HANDOFF.md`. For symptom-driven use, start with
`../docs/playback-troubleshooting.md`.

| Experiment | What it established | Where to use it |
|---|---|---|
| Six-stem skew probe | Independent media elements can stay within the measured synchronization limit when coordinated by the playback engine. | Investigating drift, doubled audio, or a seek that settles out of sync. |
| Signed media and Range | Private S3, CloudFront authorization, CORS, and HTTP 206 compose correctly. | Investigating 403 responses, missing media, or seek failures. |
| Demucs gain/reconstruction | Per-stem rescaling changes relative balance; float lossless stems plus one common gain preserve it. | Investigating gain shifts, poor reconstruction, clipping, or separation output. |
| AAC comparison | AAC-LC is the browser delivery derivative; decoded overshoot requires master protection. | Investigating audible encoding artifacts or limiter behavior. |
| Frontend deployment | The public HTTPS application, API, authentication, library, media, and mixer worked outside-in. | Investigating deployment, routing, or authentication regressions. |
| VPS and domain checks | Caddy, DNS, TLS, compose, and service health were established. | Investigating application reachability or service health. |
| RunPod endpoint checks | The worker pipeline completed on a GPU, while the cloud endpoint exposed worker-startup failures. | Investigating fresh separation or RunPod failures. |

## Detailed records

- [`RESULTS-0.1.md`](RESULTS-0.1.md) — decoder-clock and skew experiment.
- [`RESULTS-0.2.md`](RESULTS-0.2.md) — private signed media and Range.
- [`RESULTS-0.3-0.4.md`](RESULTS-0.3-0.4.md) — separation gain,
  reconstruction, and AAC derivatives.
- [`RESULTS-frontend-deploy.md`](RESULTS-frontend-deploy.md) — early outside-in
  deployment checks.
- [`RESULTS-domain-cdn.md`](RESULTS-domain-cdn.md) — domain and CDN setup.
- [`RESULTS-vps.md`](RESULTS-vps.md) — VPS service setup and health.
- [`RESULTS-runpod-endpoint.md`](RESULTS-runpod-endpoint.md) — GPU worker and
  endpoint diagnosis.
- [`RESULTS-legacy-import.md`](RESULTS-legacy-import.md) — one-time seed-library
  import and publisher behavior.

## Current boundary

The 27-track playback library and `shizzle-browser-v1` delivery path are
finished. The active work is URL/upload acquisition and dependable cloud GPU
separation into six clean lossless stems. Experiments in this directory are
consulted only when diagnosing a matching issue.
