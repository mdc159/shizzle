# Shizzle — Cloud Karaoke Stem Mixer

Paste a YouTube URL → a serverless GPU splits the song into six stems (vocals,
drums, bass, guitar, piano, shizzle) → S3 + CloudFront serve it anywhere → any
browser plays the video with live per-stem faders → an iPad pairs by QR code as
a pure touch mixing surface.

Private tool for Mike and a couple of musician friends. Not a product.

## Status

**Planning complete — implementation not started.** Successor to the k25 repo
lineage (`k25`, `k25-rewrite-mvp`, `k25-nextgen-rewrite`); good parts are copied
in with provenance, git histories deliberately left behind.

| Document | Purpose |
|---|---|
| [`docs/superpowers/specs/2026-08-02-shizzle-cloud-karaoke-design.md`](docs/superpowers/specs/2026-08-02-shizzle-cloud-karaoke-design.md) | Approved design spec (v2, post-review) |
| [`review-01.md`](review-01.md) | External architecture review folded into the spec |
| [`specs/shizzle-cloud-karaoke-implementation.html`](specs/shizzle-cloud-karaoke-implementation.html) | Implementation plan — 8 phases, open it in a browser |

## Planned layout

```
server/    FastAPI control plane + durable orchestrator (VPS)
worker/    RunPod serverless GPU worker (Demucs + encoding)
ui/        React player + remote mixing surface
infra/     Compose, Caddy, CloudFront-as-code, provisioning
spikes/    Phase-0 risk experiments and their recorded results
fixtures/  Small rights-safe test media
docs/      Specs, provenance ledger
```

## Configuration

Copy `.env.example` to `.env` and fill it in — every variable is documented
inline, including which service consumes it and two machine-specific gotchas
(the global `AWS_ENDPOINT_URL` → R2 redirect, and the dead RunPod key).
