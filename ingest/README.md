# ingest/ — source acquisition

This stage acquires source material for the pipeline. It is the active,
unfinished part of the system; this directory currently holds its contract
and state of truth while the implementation lives in the library API.

## Current truth

- **Upload is the only working entry.** The player's Add Source dialog
  streams a video file to `POST /api/upload`
  (`library/src/shizzle_server/api/routes.py`): chunked write with a hard
  2 GB cap, ffprobe duration gate, recorded sha256, and a pending job row.
  The orchestrator's `downloading` stage
  (`library/src/shizzle_server/orchestrator/stages.py`) treats an uploaded
  source as already present.
- **URL/YouTube ingestion is a deliberate stub.** `POST /api/submit-url`
  accepts the request, and the job then fails at the downloading stage with
  a structured `YTDLP_BLOCKED` error ("upload the file directly"). There is
  no URL field in the player UI.

## Requirements for the real implementation

- Acquisition must run entirely in cloud infrastructure and land the source
  object in the private bucket before dispatch.
- Provider bot controls are about network position: download success from a
  residential machine is misleading evidence and does not prove the cloud
  path. Test acquisition from the infrastructure that will run it.
- Upload remains a first-class entry regardless of URL support.

When the implementation lands, its code moves here; until then this
directory is the contract, not the code.
