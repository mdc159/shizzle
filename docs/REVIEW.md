# Current-state review

Reviewed 2026-09-04 against source revision
`885c22992eb9f570c9b0c28fcbd7c5c44b69a9f4`. Documentation and cleanup are on
`docs/current-state-review`; application fixes are deliberately deferred.
This is a source and bounded runtime review, not a guarantee of defect-free
operation or an acceptance certificate for every deployment.

## Outcome and decisions

The implemented upload-to-cloud-to-browser design is coherent, but passing
unit tests hide material correctness gaps. Five P1 findings merit priority:
lease ownership during failure/retry, ignored manifest gains, worker replay
corruption, destructive importer reruns, and migration subprocess database
selection. The remaining findings cover playback, queue timing, health reporting,
publication checks, authentication recovery, and test coverage.

Nineteen new issues were filed in the actual repository. Each contains
source evidence, reproduction or a concrete failing path, impact, and proposed
acceptance criteria. Existing issue #9 was left unchanged. No runtime bug fix,
production upload, library mutation, deployment, commit, or push was performed.

| Issue | Priority | Finding |
|---|---|---|
| [#19](https://github.com/mdc159/shizzle/issues/19) | P1 | Stale orchestrator can fail or retry a job after another worker reclaims its lease |
| [#20](https://github.com/mdc159/shizzle/issues/20) | P2 | Queue-to-running transition cancels a healthy RunPod worker after a valid queue wait |
| [#21](https://github.com/mdc159/shizzle/issues/21) | P2 | Busy orchestrator stops updating its liveness heartbeat during long stages |
| [#22](https://github.com/mdc159/shizzle/issues/22) | P2 | Valid silent or sparse AAC stems fail new-generation publication on minimum measured bitrate |
| [#23](https://github.com/mdc159/shizzle/issues/23) | P2 | Invariant documentation disagrees with heartbeat behavior and overstates several guarding tests |
| [#24](https://github.com/mdc159/shizzle/issues/24) | P2 | Review-goal renderer tests fail on native Windows POSIX executable-bit assertions |
| [#25](https://github.com/mdc159/shizzle/issues/25) | P1 | Player discards manifest default_gain_db when applying mixer state |
| [#26](https://github.com/mdc159/shizzle/issues/26) | P2 | Delayed manifest response can replace the currently selected track's media |
| [#27](https://github.com/mdc159/shizzle/issues/27) | P2 | Space shortcut changes playback state without starting the media engine |
| [#28](https://github.com/mdc159/shizzle/issues/28) | P2 | Intentional zero master volume is classified as render silence and triggers recovery |
| [#29](https://github.com/mdc159/shizzle/issues/29) | P2 | Completed cloud uploads offer a Download Multi-Track link that cannot succeed |
| [#30](https://github.com/mdc159/shizzle/issues/30) | P2 | Expired or revoked device tokens leave users unable to return to the passcode gate |
| [#31](https://github.com/mdc159/shizzle/issues/31) | P2 | Upload metadata browser tests fail on overlapping Library and Add Source dialogs |
| [#32](https://github.com/mdc159/shizzle/issues/32) | P1 | Same-dispatch worker replay can overwrite stems beneath a completed handoff |
| [#33](https://github.com/mdc159/shizzle/issues/33) | P1 | Rerunning the legacy importer resets active generations and undeletes tracks without an activation event |
| [#34](https://github.com/mdc159/shizzle/issues/34) | P1 | Migration coordinator fails to propagate --database-url to audit and activation subprocesses |
| [#35](https://github.com/mdc159/shizzle/issues/35) | P2 | Offline VM playback harness cannot reproduce acceptance with its current pipeline and fixture |
| [#36](https://github.com/mdc159/shizzle/issues/36) | P2 | Library type checking reports 24 errors while CI ignores the failure |
| [#37](https://github.com/mdc159/shizzle/issues/37) | P2 | Player development dependency lockfile has 14 npm audit findings |

Review these issues before choosing implementation batches. In particular, do
not rerun the importer or generation migration against production until their
data-integrity issues are resolved.

## Verification

| Check | Result in this review |
|---|---|
| Library unit suite, excluding Postgres | 404 passed; 11 Postgres tests deselected |
| Stemsplit unit suite | 47 passed |
| Library and stemsplit Ruff | Passed |
| Library mypy | 24 errors in 8 files; this check is informational in current CI |
| Full ops test collection | 46 passed, 1 failed on Windows POSIX executable-bit semantics; 3 passing subtests |
| Metadata normalization subset | 24 passed; included in the ops collection, not additional unique tests |
| Player dependency install and production build | Passed |
| Player ESLint and Knip | Passed; Knip reports four unused exports under its warning configuration |
| Four selected mocked browser specs | 45 passed, 2 failed: overlapping-dialog locator ambiguity, issue #31 |
| npm dependency audit | 14 development dependency advisories; production-only audit reported none |
| Invariant inventory | All 48 numbered statements preserved; all 82 named Python guard functions exist. Existence does not establish sufficient coverage; see #23. |
| Mermaid | All seven current diagrams parsed, rendered, and visually inspected; relationships traced to current implementation |
| Markdown | Relative file links checked across repository Markdown outside installed agent skills |

The selected mocked browser specs were library scrolling, remote mixer,
source-title parity, and upload metadata. The
[testing guide](TESTING.md) gives their exact commands and explains which
additional specs require live services or retain stale assumptions.

Targeted reproductions also exercised the worker, importer, and migration
coordinator with external effects mocked. A repeated worker dispatch left an
old handoff describing a mixture of new and old stem objects. An importer rerun
changed generation 3 to 1, cleared deletion state, replaced metadata, and added
no activation event. The coordinator's explicit database differed from the
database inherited by its audit/activation children. A local FFmpeg experiment
confirmed that requested AAC bitrate does not guarantee the minimum measured
bitrate for silent/sparse stems. These were not production experiments.

### Live production browser

Used the authenticated site at <https://shizzle.systems> on Windows Chrome
152.0.0.0. The library displayed 27 tracks; that is an observation from this
run, not a fixed inventory. The deployed source/image identity was not
independently established, so these observations are separate from the
source-revision test results above.

For **The Pot — Tool**, exercised initial play, repeated large forward/backward
seeks, pause/resume, natural end/replay, master zero/restoration, and a second-tab
remote mute/unmute. A replay traversed the approximately 6:18 track without
manual seeking and reached its natural end at 378.633333 seconds.

- Settled samples had maximum absolute stem/video skew of 2–3 ms, all six media
  errors null, a running AudioContext, and measured nonzero output PCM.
- Rapid seek bursts produced buffering/recovery events. Subsequent sampled
  playback was healthy; no stall bailout was reported.
- The full replay had one recovered `video-clock-stalled` incident, reporting
  video paused while playback was requested, while using another tab. Cause
  was not isolated. This run must not be described as uninterrupted fault-free
  playback; repeat foreground/background acceptance when investigating it.
- Space toggled the UI to Pause while video remained paused at approximately
  330.841 seconds. Button play resumed normally: production confirmation of #27.
- Master zero caused five `render-silence` recovery incidents despite intentional
  silence: production confirmation of #28.
- Remote mute changed the actual vocals gain; unmute returned it to 1. Master
  was restored to 100%, and playback ended stopped.
- The dashboard loaded with zero jobs in flight and orchestrator alive; it
  displayed a recent ready outcome. No job was submitted or modified.

PCM and clocks are not human listening evidence. No subjective listening verdict,
full-library campaign, live network fault campaign, or complete browser/device
matrix is claimed. Follow [Testing](TESTING.md) for those procedures.

### Not verified here

No live GPU separation or paid worker provisioning was performed. The real
Postgres contract/migration suite was not run because a disposable Postgres
service was unavailable. Linux deployment and RunPod shell fault suites were
not accepted as passing: native Windows/Git Bash lacked compatible POSIX
behavior, and jq/line-ending constraints prevented an equivalent local run.
Run those checks in the supported Linux/CI environment before release.

No production database/S3 audit, infrastructure drift audit, rollback exercise,
or credential rotation was performed. Static workflow documentation does not
prove current GitHub environment approvals, RunPod pool settings, DNS, or AWS
policy state.

## Documentation and repository disposition

The README, setup guide, component docs, handoff, media contracts, deployment
runbooks, architecture, and swim lanes now describe current paths. Important
corrections include the absent URL downloader/webhook, separate RunPod API and
worker responsibilities, current migration head, manifest-last publication,
non-baked default gains, video Blob staging, actual remote synchronization,
and docs-only deployment classification.

Stem optimization scripts, measured codec/gain results, listening instructions,
browser scrubbing/stress/natural/fault procedures, fixtures, and existing tests
are retained and linked from [Testing](TESTING.md). Historical acceptance counts
are explicitly labeled snapshots, not current proof.

Removed obsolete review/incident/source-purge narratives, the unused React
scaffold asset, and a tracked 54,996,327-byte downloaded model checkpoint.
The checkpoint work directory and model cache extension are now ignored.
The unreferenced spike-era AWS provisioning chain and executed one-shot
The Pot repair script were also removed. Current DNS data and reusable CDN
validation guidance remain; no complete infrastructure provisioner is claimed.
The dated metadata repair document was replaced by a reusable
[metadata maintenance guide](library-metadata.md). Minimal provenance and
manifest compatibility documentation remain because the source still includes
the corresponding implementations.

Removed tracked items remain recoverable from Git; repository history was not
rewritten. Reference implementations and old fixtures that still have callers
or tests were not deleted merely because they are not the production path.
Known limitations of retained tooling are explicit in their runbooks and issues.
