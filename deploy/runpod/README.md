# RunPod GPU worker

The current worker image runs `stemsplit/lossless_handler.py`: an S3 source
object becomes six aligned float32 WAV stems and a final `handoff.json`.
The VPS verifies that package, derives browser media, and publishes it. See
[the handoff contract](../../stemsplit/MANIFEST.md).

## Build and endpoint configuration

`.github/workflows/worker-image.yml` builds `stemsplit/Dockerfile.lossless` on
master pushes that change `stemsplit/**` or the worker workflow itself, and on
manual dispatch. It publishes `ghcr.io/mdc159/shizzle/worker:sha-<full-commit>`.
It does not update a RunPod endpoint. Worker builds run independently of the
four application CI checks; verify those checks before selecting an image.

The image includes CUDA 12.1, PyTorch, `htdemucs_6s`, FFmpeg, and model weights.
Its offline build step proves weights load without network access. Runtime
still needs object storage and RunPod progress reporting; the image does not
enforce an outbound network policy.

Configure a serverless endpoint/template with:

- The published worker image, pinned by digest through the repoint workflow.
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_REGION` for the source
  and attempt-output bucket. The handler's client defaults to `us-east-1`.
- Sufficient container disk for the source, decoded audio, six float32 stems,
  temporary files, and model runtime; measure peak usage on a representative
  track before setting the limit.
- GPU capacity and an execution timeout established by an actual separation
  measurement. CPU unit tests do not establish GPU memory or runtime limits.

On the VPS, set `SHIZZLE_PIPELINE=cloud`, `RUNPOD_API_KEY`, and
`RUNPOD_ENDPOINT_ID` in `/opt/shizzle/prod/.env`. Endpoint allocation is a
separate setting: a zero worker maximum prevents cloud progress even when
credentials are valid. The orchestrator also has queue/stall watchdogs.

## Conditional receipt writes

The worker claims an attempt prefix with `dispatch.json` PUT `If-None-Match: *`
(S3 conditional writes). Stores without that support must set
`SHIZZLE_CONDITIONAL_DISPATCH=0` in the worker environment: the receipt PUT
then goes unconditional (a warning is logged) and the replay guard degrades
to the completion check alone.

## Repoint an endpoint

Use the manual `runpod-repoint.yml` workflow with `image_tag`, `workers_max`,
`endpoint_id`, and the endpoint's **current** `template_id`. Workflow defaults
include endpoint `tevdw8022hs8hn` and template `vh76gbm3uy`; these are configuration
defaults, not a live inventory. Read the endpoint before each update. A
successful update changes its template ID, so the old default will then fail
the source-template preflight.

The workflow resolves the SHA tag to its registry digest, clones the specified
template with that digest, and updates the endpoint's template and worker
maximum in one request. Save the reported prior/new template IDs for recovery.
The old template is retained. If an update response is lost or ambiguous, the
new template is retained too; reconcile endpoint state before deleting anything.
Rollback requires an explicit endpoint update to the retained prior template
and the intended worker limit.

`workers_max` defaults to `0`. Raising it permits metered GPU work; image
publishing alone does not allocate a worker. Do not use an old package/tag from
historical notes as evidence of the endpoint's current image.

## Existing verification procedures

CPU-safe tests from the repository root:

```powershell
uv run --directory stemsplit pytest -q
uv run --directory stemsplit ruff check .
```

The repoint failure-path suite uses stubbed registry and RunPod commands;
run with Bash and `jq` available:

```sh
bash deploy/runpod/tests/test_repoint.sh
```

For an explicit local GPU proof, use a published image and an empty output
directory. On a Docker host with NVIDIA GPU support:

```sh
docker run --rm --gpus all --network none \
  -v /absolute/source-directory:/in:ro -v /absolute/empty-output-directory:/out \
  ghcr.io/mdc159/shizzle/worker@sha256:<digest> \
  python3 lossless_handler.py --local /in/source.mp4 /out --track-id <uuid>
```

This checks real offline separation without S3. Validate the resulting schema,
six roles, hashes, stereo 44.1 kHz float32 format, identical sample counts,
elapsed phases, disk use, and GPU memory before sizing a cloud pool. A separate
authorized cloud test must prove source download, the dispatch receipt, six
uploads, handoff-last completion, VPS intake, and browser publication. Retain
[playback and scrubbing procedures](../../docs/playback-troubleshooting.md)
when validating the derived browser generation.
