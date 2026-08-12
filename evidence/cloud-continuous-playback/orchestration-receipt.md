# Cloud continuous playback orchestration receipt

Date: 2026-08-05

> Historical receipt only. It records one intermediate implementation turn and
> is not a current status or requirements document. Current state is in
> `goal.md`, `plan.md`, and `../../docs/HANDOFF.md`.

## Control plane

- HERDR client/server: `0.8.0-preview.2026-08-04-d78e3d3b5126`, protocol 19, compatible, restart not needed.
- Pi: `0.83.0`; Pi HERDR integration current (v8).
- `HERDR_ENV`: supplied (value not recorded).
- Task workspace: `w13` (`cloud playback review`), tab `w13:t1`.
- Panes: investigator `w13:p1`; critic `w13:p2`.
- Workspace left open for continuation.
- HERDR `agent start --kind pi` hit the Windows npm-shim error (`%1 is not a valid Win32 application`); workers were launched through `pi.cmd` in the task-owned panes and then managed by HERDR.

## Workers

| Name | Pane | Model | Thinking | Skill | Tools | Authority | Final |
|---|---|---|---|---|---|---|---|
| `playback-investigator` | `w13:p1` | `kimi-coding/k3` | high | `skills/state-investigator` | read, grep, find, ls, bash | read-only; no credentials/cloud | `VERDICT=READY`, done |
| `playback-critic` | `w13:p2` | `zai/glm-5.2` | high | `skills/diff-critic` | read, grep, find, ls, bash | read-only; no credentials/cloud | initial `VERDICT=RED`; post-fix `VERDICT=GREEN`, done |

The investigator mapped requirements 1-16 and recommended the executable delivery profile as the next larger slice. The critic found the worker cloud-video path omitted the proven H.264/GOP/timeline flags and identified buffering false-recovery risk. Post-fix review found no defect in the scoped corrections.

## Parent changes in this turn

- Aligned worker video derivation with `shizzle-browser-v1`: H.264 Main 3.1, yuv420p, 30 fps, zero PTS, two-second closed GOP, fast-start.
- Pinned new stem encodes to AAC-LC, 44.1 kHz, stereo, and changed the default new lossless-derived bitrate to 256 kb/s.
- Added command-contract tests for those FFmpeg flags.
- Prevented normal buffering from triggering the 1 Hz stall recovery path.
- Prevented a stale track-load promise from marking a newer track ready.
- Added the two project-local read-only role skills used above.

No production, AWS, RunPod, VPS, credential, publication, destructive, or physical-device action was performed.

## Validation

- `uv run --directory worker pytest -q`: 42 passed.
- `uv run --directory worker ruff check .`: passed.
- `cd ui && npm run build && npm run lint`: passed.
- `uv run --directory server pytest -q -m "not postgres"`: 101 passed, 9 deselected (run before the scoped fixes; no subsequent server change).
- `uv run --directory server ruff check .`: passed.
- `git diff --check`: passed (line-ending warnings only).

## Superseded status

The open-gate list originally recorded here was completed or superseded during
the later production rollout. Browser delivery and playback are now complete
for the accepted 27-track library. This receipt remains only to preserve how the
earlier scoped changes were reviewed.
