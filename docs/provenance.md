# Source attribution

Some reference implementations and tests originated in Mike's earlier karaoke
repositories. Attribution is retained separately from current setup and design:

| Source | Retained lineage |
|---|---|
| `k25-nextgen-rewrite`, revision `fd6f3f467a42a43b379f050df3551b1d9d38c153` | React player foundations and local FFmpeg/Demucs processing |
| `k25`, revision `65b3d88ffc81bd32d0444eb356715cdb751c821c` | Reference combined worker, S3 multipart and circuit-breaker helpers and carried tests |
| `k25` working changes | Mixer mute/solo behavior ported into the player |

The current module layout and responsibilities are documented in
[Architecture](architecture.md). These source revisions identify ancestry;
they do not describe the present implementation.

`fixtures/golden-30s.mp4` is a synthetic FFmpeg-generated video/audio test
fixture. It is retained for automated and explicit cloud acceptance runs.
