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
`sources/<track-id>/source.mp4` in S3 and advances to RunPod dispatch.
See the [architecture](../docs/architecture.md).

`POST /api/submit-url` validates an HTTP(S) URL and creates a job. The downloading
stage then fails with `YTDLP_BLOCKED`; there is no URL downloader or URL input
in the player. Upload is the supported path.

See [Setup](../docs/SETUP.md) and [Testing](../docs/TESTING.md).
A successful upload response proves job creation, not separation or publication.
