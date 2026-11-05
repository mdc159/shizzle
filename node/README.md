# Shizzle worker-node agent

The agent that turns the Mac mini (and later the Dell 9530) into a worker
node for <https://shizzle.systems>. It is the node half of
[docs/WORKER-NODES.md](../docs/WORKER-NODES.md): it pulls assignments from the
control plane over outbound HTTPS, downloads YouTube sources with yt-dlp, and
runs the **production separation code from `../stemsplit` unchanged** (the
same `lossless_handler.py` the RunPod image runs), uploading the
`lossless-stem-v1` package with `handoff.json` last. The VPS verifies,
derives the browser files and publishes exactly as it does for RunPod.

Nothing about file creation changes. The node never writes under `tracks/*/1/`
except the separation attempt prefix, and never under `imports/`.

## What a node does

| Assignment | Input | Output in S3 | Proof the orchestrator checks |
|---|---|---|---|
| `download` | a YouTube URL | `sources/<track>/source.mp4`, then `sources/<track>/source.json` **last** | object key, size and sha256 in the receipt match the object |
| `separate` | `sources/<track>/source.mp4` | `tracks/<track>/1/separation/attempts/<key>/{dispatch.json, stems/*.wav, handoff.json last}` | `handoff.json` present; re-proven by `cloud_verifying` |

One assignment at a time per node. A crash mid-job leaves no completion
marker; the orchestrator expires the assignment and redispatches.

## Install on the Mac mini (first node)

Requirements: Python 3.11 via `uv`, `ffmpeg`/`ffprobe` on PATH (already there),
Docker is **not** needed for the agent. Separation runs natively on the Apple
GPU (`mps`); Docker Desktop cannot reach it.

```sh
cd /path/to/shizzle          # a checkout of mdc159/shizzle (this repo)
uv sync --directory node --python 3.11
```

That pulls torch/torchaudio (the default macOS build has Metal support),
`demucs==4.1.0`, yt-dlp, boto3 and httpx into `node/.venv`. The first
separation downloads the `htdemucs_6s` weights (~80 MB) into the torch cache.

Environment (put it in the launchd plist or a vault-managed env, never in a
committed file):

```sh
SHIZZLE_API_BASE=https://shizzle.systems
SHIZZLE_NODE_ID=mac-mini                 # must match an entry in SHIZZLE_NODE_TOKENS on the VPS
SHIZZLE_NODE_TOKEN=<long random token>
SHIZZLE_NODE_DISPLAY_NAME="Mac mini"
SHIZZLE_NODE_DEVICE=mps                  # auto | mps | cuda | cpu
SHIZZLE_NODE_CAPABILITIES=download,separate
SHIZZLE_NODE_WORKDIR=$HOME/.shizzle-node
AWS_ACCESS_KEY_ID=...                    # the node's scoped IAM user (below)
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
# optional: SHIZZLE_YTDLP_COOKIES=/path/to/youtube-cookies.txt
```

Run it:

```sh
uv run --directory node shizzle-node
```

You should see the node appear on `/dashboard` within one heartbeat (15 s).
Then paste a YouTube link into the player's Add Source dialog.

### Keep it running (launchd)

`~/Library/LaunchAgents/com.shizzle.node.plist`, then
`launchctl load ~/Library/LaunchAgents/com.shizzle.node.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.shizzle.node</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/seabass/.local/bin/uv</string>
    <string>run</string><string>--directory</string>
    <string>/Users/seabass/Documents/Projects/shizzle/node</string>
    <string>shizzle-node</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SHIZZLE_API_BASE</key><string>https://shizzle.systems</string>
    <key>SHIZZLE_NODE_ID</key><string>mac-mini</string>
    <key>SHIZZLE_NODE_DEVICE</key><string>mps</string>
    <!-- token and AWS keys: inject from the vault, do not paste here -->
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/shizzle-node.log</string>
  <key>StandardErrorPath</key><string>/tmp/shizzle-node.log</string>
</dict></plist>
```

## Install on the Dell 9530 (Windows, RTX 4070)

Same agent; after the sync, swap in the CUDA build of torch (the PyPI
build on Windows is CPU-only):

```powershell
cd C:\path\to\shizzle
uv sync --directory node --python 3.11
uv pip install --directory node --python node\.venv --reinstall `
  --index-url https://download.pytorch.org/whl/cu121 torch==2.2.2+cu121 torchaudio==2.2.2+cu121
$env:SHIZZLE_NODE_ID = "dell-9530"; $env:SHIZZLE_NODE_DEVICE = "cuda"   # plus the rest
uv run --directory node --no-sync shizzle-node
```

`--no-sync` keeps `uv run` from putting the CPU build back. Verify with
`uv run --directory node --no-sync python -c "import torch; print(torch.cuda.is_available())"`.

`ffmpeg`/`ffprobe` must be on PATH (winget `Gyan.FFmpeg`). Register it as a
service with NSSM or a Task Scheduler "at startup" task once it works by hand.

## Server side

On the VPS `.env`:

```sh
SHIZZLE_NODE_ROUTING=prefer                     # prefer | only | off
SHIZZLE_NODE_TOKENS=mac-mini=<token>,dell-9530=<token>
```

Generate a token with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
Both `api` and `orchestrator` read the same `.env`, so one edit and a restart
of both services.

### IAM policy for a node

The node needs only the source prefix and the separation attempt prefix:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:HeadObject"],
      "Resource": [
        "arn:aws:s3:::karaoke-pimpshizzle/sources/*",
        "arn:aws:s3:::karaoke-pimpshizzle/tracks/*/1/separation/attempts/*"
      ]
    }
  ]
}
```

No RunPod, CloudFront, database or deploy credentials ever live on a node.

## Tests

```sh
uv sync --directory node --no-default-groups --group dev   # no torch needed
uv run --directory node pytest -q
uv run --directory node ruff check src tests
```

The tests use a fake control plane and injected executors; nothing touches
the network, yt-dlp or torch. The real proof is one URL through
`shizzle.systems` with the node online and `fake.dispatched == []` on RunPod,
recorded under `evidence/` when it happens.
