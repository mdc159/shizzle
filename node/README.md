# Shizzle worker-node agent

The agent that makes the owner's machines worker nodes for
<https://shizzle.systems>. Two run today: `mac-mini` (native, Apple `mps`)
and `dell-9530` (a CUDA container in WSL). It is the node half of
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

## Install on the Mac mini (native)

Requirements: Python 3.11 via `uv`, `ffmpeg`/`ffprobe` on PATH (already there),
Docker is **not** needed for the agent. Separation runs natively on the Apple
GPU (`mps`); Docker Desktop cannot reach it.

```sh
cd /path/to/shizzle          # a checkout of mdc159/shizzle (this repo)
uv sync --locked --directory node --python 3.11
```

That installs exactly what `node/uv.lock` pins: torch/torchaudio 2.2.2 (the
same version as the RunPod image; the macOS build has Metal support),
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

The secrets live in one root-only env file that a wrapper script sources;
the plist itself holds nothing sensitive. Replace `<you>` and the checkout
path with your own.

`~/.config/shizzle-node/env` (`chmod 600`), written from the vault:

```sh
SHIZZLE_API_BASE=https://shizzle.systems
SHIZZLE_NODE_ID=mac-mini
SHIZZLE_NODE_TOKEN=<long random token>
SHIZZLE_NODE_DISPLAY_NAME="Mac mini"
SHIZZLE_NODE_DEVICE=mps
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
```

`~/.config/shizzle-node/run.sh` (`chmod 700`). The `PATH` line is required:
launchd starts jobs with `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, which hides
Homebrew's `ffmpeg`. Without it every download fails with "You have requested
merging of multiple formats but ffmpeg is not installed" (yt-dlp joins
YouTube's separate video and audio streams with ffmpeg).

```sh
#!/bin/sh
# launchd starts with PATH=/usr/bin:/bin:/usr/sbin:/sbin; yt-dlp and the
# separator need Homebrew ffmpeg/ffprobe and uv.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
set -a; . "$HOME/.config/shizzle-node/env"; set +a
exec "$HOME/.local/bin/uv" run --directory "$HOME/Projects/shizzle/node" shizzle-node
```

`~/Library/LaunchAgents/com.shizzle.node.plist`, then
`launchctl load ~/Library/LaunchAgents/com.shizzle.node.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.shizzle.node</string>
  <key>ProgramArguments</key>
  <array><string>/Users/<you>/.config/shizzle-node/run.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- The agent finishes its current job on SIGTERM (a second signal exits
       at once); launchd's default 20 s would cut that short. -->
  <key>ExitTimeOut</key><integer>1800</integer>
  <key>StandardOutPath</key><string>/tmp/shizzle-node.log</string>
  <key>StandardErrorPath</key><string>/tmp/shizzle-node.log</string>
</dict></plist>
```

### Updating the Mac

```sh
cd ~/Projects/shizzle && git pull --ff-only
uv sync --locked --directory node --python 3.11
launchctl kickstart -k gui/$(id -u)/com.shizzle.node   # finishes a running job first
```

## Install on the Dell 9530 (Windows, RTX 4070): container in WSL

The Dell runs the agent as a Docker container inside WSL (Ubuntu-24.04, Docker
Engine, NVIDIA container toolkit; no Docker Desktop). The image is built
locally on top of the production lossless worker image already on the
machine, which brings CUDA torch 2.2.2, demucs 4.1.0 with the `htdemucs_6s`
weights baked in, and ffmpeg. The build adds only the agent and yt-dlp, and
copies in `node/` and `stemsplit/` from a checkout, so the container runs
master's `lossless_handler.py`, the same file RunPod and the Mac run.
Nothing here goes through GitHub Actions or the production deploy.

Layout in WSL, `~/shizzle-node/`:

| Path | What |
|---|---|
| `shizzle/` | clone of `mdc159/shizzle` master |
| `Dockerfile` | below |
| `.dockerignore` | `*`, `!shizzle/node`, `!shizzle/stemsplit` |
| `update.sh` | below: pull, rebuild, restart |

The base tag is the local worker image, tagged once:
`docker tag <worker image id> shizzle-worker-base:2026-09-24`.

`Dockerfile`:

```dockerfile
FROM shizzle-worker-base:2026-09-24
COPY shizzle/node /opt/shizzle-node/node
COPY shizzle/stemsplit /opt/shizzle-node/stemsplit
RUN pip3 install --no-cache-dir "yt-dlp==2026.10.26" "httpx>=0.28" \
 && pip3 install --no-cache-dir --no-deps /opt/shizzle-node/node
ENV SHIZZLE_STEMSPLIT_DIR=/opt/shizzle-node/stemsplit \
    SHIZZLE_NODE_WORKDIR=/work \
    PYTHONUNBUFFERED=1
WORKDIR /opt/shizzle-node
CMD ["shizzle-node"]
```

`update.sh` (also the first install):

```sh
#!/bin/sh
set -e
cd "$HOME/shizzle-node"
git -C shizzle pull --ff-only -q
docker build -q -t shizzle-node:local .
docker stop -t 1800 shizzle-node >/dev/null 2>&1 || true   # lets a running job finish
docker rm shizzle-node >/dev/null 2>&1 || true
docker run -d --name shizzle-node --restart unless-stopped --gpus all \
  --stop-timeout 1800 \
  --env-file /mnt/c/Users/Mike/.config/shizzle-node/env \
  shizzle-node:local
```

The env file is `C:\Users\Mike\.config\shizzle-node\env` on the Windows side,
in the same `KEY=value` form as the Mac's, with `SHIZZLE_NODE_ID=dell-9530`
and `SHIZZLE_NODE_DEVICE=cuda`. Docker reads values literally, so write
`SHIZZLE_NODE_DISPLAY_NAME=Dell 9530` without quotes.

WSL stops when nothing holds it open, which would stop Docker and the node.
A Windows Task Scheduler task, "WSL keepalive (shizzle node, firecrawl)",
runs at logon and keeps the distro up with no window:
`conhost.exe --headless wsl.exe -d Ubuntu-24.04 --exec /bin/sleep infinity`.

Useful checks from Windows:

```powershell
wsl -d Ubuntu-24.04 -- docker logs --tail 20 shizzle-node
wsl -d Ubuntu-24.04 -- docker ps --filter name=shizzle-node
```

To test without production, run the image with `--entrypoint bash` and call
`download_source(...)` from `shizzle_node.download`, then
`python3 lossless_handler.py --local <source> <outdir>` in
`/opt/shizzle-node/stemsplit`. On 2026-11-05 that took 4.7 s to download a
5:03 track and 12.4 s to separate it on the RTX 4070.

The GPU has 8 GB and is shared with any other local GPU containers; keep
them stopped while the node may be separating.

## Adding a node

1. On the VPS, generate a token
   (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`), append
   `,<node-id>=<token>` to `SHIZZLE_NODE_TOKENS` in `/opt/shizzle/prod/.env`
   (back the file up first), and recreate the two services on the pinned
   image: `docker compose -f compose.prod.yml up -d --no-deps api orchestrator`.
2. Create IAM user `shizzle-node-<node-id>` with the inline policy below
   (`shizzle-node-s3`) and one access key.
3. Write the token and the key into the node's env file only, then start the
   agent. The node appears on `/dashboard` within 15 seconds.

When copying keys out of Windows `aws.exe --output text`, strip the trailing
carriage return; a stray `\r` on the secret fails every request with
`SignatureDoesNotMatch`.

## Server side

On the VPS `.env`:

```sh
SHIZZLE_NODE_ROUTING=prefer                     # prefer | only | off
SHIZZLE_NODE_TOKENS=mac-mini=<token>,dell-9530=<token>
```

Production sets both lines with tokens for `mac-mini` and `dell-9530`.
Both `api` and `orchestrator` read the same `.env`, so one edit and a
recreate of both services; see "Adding a node" above.

### IAM policy for a node

The node needs only the source prefix and the separation attempt prefix:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": [
        "arn:aws:s3:::karaoke-pimpshizzle/sources/*",
        "arn:aws:s3:::karaoke-pimpshizzle/tracks/*/1/separation/attempts/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::karaoke-pimpshizzle",
      "Condition": {"StringLike": {"s3:prefix": ["sources/*", "tracks/*/1/separation/attempts/*"]}}
    }
  ]
}
```

HEAD requests are authorized by `s3:GetObject` (there is no `s3:HeadObject`
action). `s3:ListBucket` on those prefixes makes a missing key answer 404
rather than 403, which the worker's "is it there yet" checks rely on.

This policy covers every job's source and attempt prefix, not only the
current assignment's: accepted for the owner's own machines. Writes are
conditional (first writer wins), so a stale node cannot overwrite a source or
package the VPS already accepted. No RunPod, CloudFront, database or deploy
credentials ever live on a node.

## Tests

```sh
uv sync --locked --directory node --no-default-groups --group dev   # no torch needed
uv run --directory node pytest -q
uv run --directory node ruff check src tests
```

The tests use a fake control plane and injected executors; nothing touches
the network, yt-dlp or torch. The real proof is one URL through
`shizzle.systems` with the node online and `fake.dispatched == []` on RunPod,
recorded under `evidence/` when it happens.
