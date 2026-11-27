# Source acquisition

This directory documents ingestion; it contains no separate service.
Implementation: [routes.py](../library/src/shizzle_server/api/routes.py) and
[stages.py](../library/src/shizzle_server/orchestrator/stages.py).

`POST /api/upload` accepts multipart video and optional title/artist,
saves `DATA_DIR/<job-id>/source.mp4`, computes SHA-256, and creates a pending
job. Its copy loop enforces `MAX_UPLOAD_BYTES` (default 2 GiB). A successful
duration probe exceeding `MAX_DURATION_SECONDS` (1800) is rejected;
probe failures currently fall through to pipeline validation. The multipart
parser receives the upload before that copy loop runs.

In cloud mode, the downloading stage uploads the source to
`sources/<track-id>/source.mp4` in S3 and advances to separation dispatch
(a worker node first, RunPod as fallback).
See the [architecture](../docs/architecture.md).

`POST /api/submit-url` accepts a link to one YouTube video (the player's Add
Source dialog has a URL field) and creates a job. The VPS never downloads it:
the downloading stage assigns the URL to an online worker node, which fetches
it with yt-dlp and writes `sources/<track-id>/source.mp4`, then
`source.json` last. With no node online the job waits up to 24 hours, then
fails `NO_NODE_AVAILABLE`. Any other kind of URL, or
`SHIZZLE_NODE_ROUTING=off`, fails `YTDLP_BLOCKED`. See
[Worker nodes](../docs/WORKER-NODES.md).

See [Setup](../docs/SETUP.md) and [Testing](../docs/TESTING.md).
A successful upload response proves job creation, not separation or publication.
