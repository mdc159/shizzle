# Shizzle Invariants

This file is the single source of review truth for the shizzle repo. Consumers:
humans doing code review, the AI reviewers (CodeRabbit ingests this file via its
knowledge base; Greptile and cubic receive mirrored rules — see
[docs/AUTOMATION.md](AUTOMATION.md)), and fleet builder agents that must not
regress these properties while refactoring.

The IDs are stable. They are cited in code review ("violates B3") and in
`.coderabbit.yaml` path instructions. A PR that changes an invariant MUST
update this file AND its guarding test in the same PR. Line numbers under
`Where:` drift as code moves; the guarding test is the authoritative anchor.

Unguarded invariants are explicitly marked; they are wanted-test candidates,
not optional properties.

## A. Handoff / interface contract (lossless-stem-v1)

### A1 — handoff.json is written last

**Invariant:** The worker MUST upload `handoff.json` only after every object it
references is durably in S3. A visible handoff implies a complete attempt.
- Where: `interfaces/lossless-stem-v1/spec.md:118`, `stemsplit/lossless_handler.py:108`, `library/src/shizzle_server/publish/lossless_intake.py:69`
- Guarded by: `library/tests/test_lossless_intake.py::test_download_package_missing_handoff_raises`, `library/tests/test_orchestrator_unit.py::test_cloud_verifying_missing_handoff_is_retryable`
- Violation smell: any upload or rename of `handoff.json` before the final stem
  PUT returns.

### A2 — exactly six stems, sixth role `shizzle`

**Invariant:** A lossless-stem-v1 package MUST contain exactly six canonical
stem roles — `vocals, drums, bass, guitar, piano, shizzle` — with the sixth role
named `shizzle` (renamed from the separator's `other`); no other role set is
accepted.
- Where: `interfaces/lossless-stem-v1/spec.md:53`, `stemsplit/lossless_worker.py:39`, `library/src/shizzle_server/publish/delivery_profile.py:18`, `library/src/shizzle_server/publish/lossless_intake.py:219`
- Guarded by: `library/tests/test_delivery_profile.py::test_manifest_requires_six_canonical_roles_and_paths`, `library/tests/test_lossless_intake.py::test_load_rejects_duplicate_roles_before_deduplication`
- Violation smell: adding or renaming a role in one place (worker, profile,
  intake) without the other two.

### A3 — stems are sample-identical lossless stereo

**Invariant:** Every stem MUST be stereo, 44100 Hz, `pcm_f32le`, starting at
sample 0, with identical sample counts across all six stems; no per-stem
normalization or lossy encode may be applied.
- Where: `interfaces/lossless-stem-v1/spec.md:56`, `stemsplit/lossless_worker.py:125`, `library/src/shizzle_server/publish/lossless_intake.py:210`
- Guarded by: `library/tests/test_lossless_intake.py::test_load_rejects_actual_stem_sample_count_mismatch`
- Violation smell: any `loudnorm`, per-stem gain, or non-float32 WAV write in
  the worker.

### A4 — handoff claims are re-proven against bytes

**Invariant:** Every handoff claim MUST be re-proven against the actual bytes
(size + sha256 + ffprobe); the handoff document is never trusted.
- Where: `library/src/shizzle_server/publish/lossless_intake.py:186`, `library/src/shizzle_server/publish/lossless_intake.py:232`
- Guarded by: `library/tests/test_orchestrator_unit.py::test_cloud_verifying_rejects_source_hash_mismatch`, `library/tests/test_orchestrator_unit.py::test_cloud_verifying_rejects_source_object_mismatch`
- Violation smell: reading a hash or duration from the handoff/manifest instead
  of recomputing it from the downloaded object.

### A5 — stem paths cannot escape the package

**Invariant:** Declared stem paths MUST NOT escape the package directory.
- Where: `library/src/shizzle_server/publish/lossless_intake.py:134`, `library/src/shizzle_server/publish/lossless_intake.py:179`
- Guarded by: `library/tests/test_lossless_intake.py::test_download_package_rejects_path_traversal`, `library/tests/test_lossless_intake.py::test_verification_path_containment_rejects_escape`
- Violation smell: joining a manifest-supplied path into the package dir without
  a containment check.

### A6 — each dispatch attempt under its own immutable prefix

**Invariant:** Each dispatch attempt MUST write beneath its own immutable
`attempts/<sha256(idempotency_key)>` prefix, so an older worker can never
clobber a newer attempt's receipts or stems.
- Where: `stemsplit/lossless_handler.py:36`, `stemsplit/lossless_handler.py:81`
- Guarded by: `stemsplit/tests/test_wave3_hardening.py::test_attempt_prefix_is_deterministic_and_isolates_retries`, `stemsplit/tests/test_wave3_hardening.py::test_handler_isolates_each_dispatch_attempt`, `library/tests/test_orchestrator_unit.py::test_legacy_unconfirmed_package_uses_base_prefix`
- Violation smell: writing any package object outside the attempt prefix, or
  deriving the prefix from anything but the idempotency key hash.

### A7 — uploaded handoff is schema-strict

**Invariant:** The uploaded handoff.json MUST be schema-strict:
`_`-prefixed private keys are stripped before upload.
- Where: `stemsplit/lossless_handler.py:31`, `stemsplit/lossless_worker.py:238`
- Unguarded; candidate for a test asserting the uploaded document contains no
  `_`-prefixed keys.

### A8 — RunPod owns nothing past the interface

**Invariant:** The RunPod worker MUST own nothing past the lossless-stem-v1
interface — no AAC encode, no video, no delivery manifest.
- Where: `interfaces/lossless-stem-v1/spec.md:35`, `stemsplit/lossless_worker.py:3`, `stemsplit/Dockerfile.lossless:4`
- Structural: enforced by what the worker image contains and what the handler
  uploads. Violation smell: an ffmpeg AAC/MP4 invocation in `stemsplit/`.

### A9 — worker model weights are baked offline

**Invariant:** The worker image MUST bake the htdemucs_6s weights at build
time: the Dockerfile's `RUN --network=none` model-load check fails the build
if any weight would need a network download.
- Where: `stemsplit/Dockerfile.lossless:7`, `stemsplit/Dockerfile.lossless:56`
- Enforced by the image build itself (the offline weights check fails the
  build if any weight is missing). Violation smell: removing the
  `--network=none` check or adding a runtime model download.

Runtime aspiration, NOT machine-enforced: at runtime the worker should reach
only object storage (plus RunPod progress reporting) and nothing else. The
build-time check proves offline model availability only — it does not impose
a network policy on containers created from the image. Real egress control
must be enforced at the RunPod/network layer.

### A10 — uploads carry locally computed sha256

**Invariant:** Every worker upload MUST carry a locally computed sha256; the
publisher verifies server-side before promotion.
- Where: `stemsplit/s3_ops.py:3`, `stemsplit/s3_ops.py:176`, `stemsplit/s3_ops.py:93`
- Guarded by: `library/tests/test_publish.py::test_staged_object_parses_worker_result_payload` (plus the staged-object verify tests in `library/tests/test_publish.py`)
- Violation smell: an upload record without a `sha256` field, or a publisher
  that promotes without re-verifying.

### A11 — transfer progress callback is lock-guarded

**Invariant:** The multipart progress callback closure is mutated by several
transfer-manager threads, so every read-modify-write MUST be lock-guarded and
progress MUST be monotonic.
- Where: `stemsplit/s3_ops.py:29`
- Guarded by: `stemsplit/tests/test_wave3_hardening.py::test_transfer_callback_is_thread_safe_and_monotonic`
- Violation smell: touching callback state without holding its lock.

## B. Orchestrator lease / dispatch

### B1 — claim uses FOR UPDATE SKIP LOCKED

**Invariant:** Job claim MUST use `SELECT ... FOR UPDATE SKIP LOCKED`; an
expired foreign lease is reclaimed with a `lease_reclaimed` event.
- Where: `library/src/shizzle_server/db/repository.py:199`
- Guarded by: `library/tests/test_orchestrator_unit.py::test_claim_respects_active_lease_and_retry_time`, `library/tests/contract/test_orchestrator_postgres.py::test_expired_foreign_lease_reclaimed_by_second_instance`, `library/tests/contract/test_orchestrator_postgres.py::test_live_lease_not_stolen`
- Violation smell: a plain SELECT claim, or reclaiming a lease that has not
  expired.

### B2 — lease ownership checked inside the locked transaction

**Invariant:** Lease ownership MUST be checked INSIDE the locked transaction:
reserve/record dispatch reject unless `status == dispatched` AND
`lease_owner == worker_id` AND the lease is unexpired; renew/release/park are
scoped the same way.
- Where: `library/src/shizzle_server/db/repository.py:276`, `library/src/shizzle_server/db/repository.py:393`, `library/src/shizzle_server/db/repository.py:240`
- Guarded by: `library/tests/test_repository.py::test_record_dispatch_requires_dispatched_stage_and_lease_owner`, `library/tests/test_repository.py::test_record_dispatch_rejects_expired_or_missing_lease`, `library/tests/test_orchestrator_unit.py::test_renew_and_release_lease_ownership`
- Violation smell: any repository mutation that trusts a job_id + worker_id
  pair without re-reading owner and expiry under lock.

### B3 — reservation commits before the external call

**Invariant:** The dispatch reservation MUST commit BEFORE the external API
call; a reclaimed lease reconciles the outstanding reservation instead of
paying for another worker, and timeout/5xx responses never trigger a second
dispatch.
- Where: `library/src/shizzle_server/db/repository.py:279`, `library/src/shizzle_server/orchestrator/stages.py:373`
- Guarded by: `library/tests/test_repository.py::test_dispatch_reservation_blocks_duplicate_and_survives_lease_turnover`, `library/tests/test_orchestrator_unit.py::test_dispatch_timeout_reservation_prevents_redispatch`
- Violation smell: calling RunPod before the reservation transaction commits, or
  a retry path that allocates a new idempotency key for a timeout.

### B4 — confirmation survives lease loss

**Invariant:** Confirmation deliberately does NOT require the original lease;
the latest reservation key MUST block stale-dispatcher overwrite.
- Where: `library/src/shizzle_server/db/repository.py:331`
- Guarded by: `library/tests/test_orchestrator_unit.py::test_accepted_dispatch_survives_confirmation_failure`, `library/tests/test_repository.py::test_keyed_confirmation_does_not_clear_legacy_pending_dispatch`
- Violation smell: keying confirmation on lease ownership, or letting an older
  dispatcher's confirmation clear a newer reservation.

### B5 — legacy dispatch_unconfirmed fails closed

**Invariant:** Legacy `dispatch_unconfirmed` events MUST fail closed across
rolling upgrades (block redispatch rather than pay for a duplicate worker).
- Where: `library/src/shizzle_server/db/repository.py:60`
- Guarded by: `library/tests/test_repository.py::test_legacy_unconfirmed_dispatch_blocks_redispatch_after_upgrade`
- Violation smell: treating an unconfirmed legacy dispatch as retryable.

### B6 — poll-failure events dedupe per outage

**Invariant:** Poll-failure events MUST dedupe to one `runpod_poll_failed` row
per outage; any other event resets the dedup window.
- Where: `library/src/shizzle_server/orchestrator/stages.py:191`, `library/src/shizzle_server/orchestrator/stages.py:207`
- Guarded by: `library/tests/test_orchestrator_unit.py::test_transient_poll_failure_dedupes_one_event_per_outage`, `library/tests/test_orchestrator_unit.py::test_successful_poll_resets_outage_even_when_phase_is_unchanged`
- Violation smell: appending a poll-failed event on every retry tick.

### B7 — cancel never masks the original error

**Invariant:** A failed RunPod cancel MUST NEVER mask the original error:
`_cancel_best_effort` logs and swallows cancel failures so the original
transient poll error propagates.
- Where: `library/src/shizzle_server/orchestrator/stages.py:44`, `library/src/shizzle_server/orchestrator/stages.py:186`
- Guarded by: `library/tests/test_orchestrator_unit.py::test_stale_worker_cancel_failure_does_not_mask_transient_poll_error`, `library/tests/test_orchestrator_unit.py::test_stale_poll_outage_cancels_and_marks_existing_job_failed`
- Violation smell: an unguarded `await runpod.cancel(...)` in an error path.

### B8 — heartbeats only on phase change

**Invariant:** Worker-phase heartbeats MUST be written only on phase change
(bounded history per job).
- Where: `library/src/shizzle_server/db/repository.py:419`
- Guarded by: `library/tests/test_repository.py::test_worker_progress_writes_only_on_phase_change`
- Violation smell: a DB write per progress tick.

### B9 — park frees the lease, costs no attempt

**Invariant:** Parking MUST free the lease WITHOUT consuming an attempt or
appending an event; stages signal it purely via `ctx.park_seconds`.
- Where: `library/src/shizzle_server/db/repository.py:259`, `library/src/shizzle_server/orchestrator/stages.py:303`
- Guarded by: `library/tests/test_repository.py::test_park_frees_lease_without_consuming_attempt_or_adding_event`, `library/tests/test_orchestrator_unit.py::test_cloud_dispatched_park_does_not_increment_attempt`, `library/tests/test_orchestrator_unit.py::test_transient_poll_failure_parks_without_consuming_attempt`, `library/tests/test_orchestrator_unit.py::test_queue_watchdog_age_survives_two_parks`
- Violation smell: incrementing `attempt` or appending an event in the park
  path.

### B10 — unresolvable dispatch identity fails closed

**Invariant:** An unresolvable dispatch identity MUST fail closed after
`queue_timeout + worker_stall` — explicit operator recovery, never silent
redispatch.
- Where: `library/src/shizzle_server/orchestrator/stages.py:324`
- Indirect coverage via the reservation tests (`test_dispatch_reservation_blocks_duplicate_and_survives_lease_turnover`, `test_dispatch_timeout_reservation_prevents_redispatch`).
- Violation smell: any automatic retry after a dispatch identity is lost.

### B11 — stage handlers idempotent under crash-rerun

**Invariant:** Stage handlers MUST be idempotent under crash-rerun (marker and
manifest guarded on disk; deterministic-id idempotent publish transaction).
- Where: `library/src/shizzle_server/orchestrator/stages.py:1`
- Guarded by: `library/tests/test_orchestrator_unit.py::test_effect_counter_stage_idempotency`, `library/tests/contract/test_orchestrator_postgres.py::test_kill_mid_stage_then_restart_resumes_exactly_once`, `library/tests/contract/test_orchestrator_postgres.py::test_two_live_instances_process_each_job_exactly_once`
- Violation smell: a stage whose re-run duplicates an event, upload, or
  reservation.

### B12 — failed RunPod job dispatches fresh

**Invariant:** A RunPod job already marked failed MUST be treated as absent —
retry dispatches fresh under a NEW idempotency key.
- Where: `library/src/shizzle_server/orchestrator/stages.py:157`, `library/src/shizzle_server/orchestrator/stages.py:171`
- Guarded by: `library/tests/test_orchestrator_unit.py::test_cloud_dispatched_runpod_failed_redispatches_fresh`
- Violation smell: reusing the old idempotency key after a RunPod-side
  failure.

## C. Publication immutability

### C1 — generations are immutable

**Invariant:** Published generations MUST be immutable and never overwritten
(CloudFront caches them); if the destination manifest already exists the
publish is a no-op returning `already_published`.
- Where: `library/src/shizzle_server/publish/publisher.py:22`, `library/src/shizzle_server/publish/publisher.py:138`, `docs/architecture.md:134`
- Guarded by: `library/tests/test_publish.py::test_published_generation_is_never_overwritten`, `library/tests/test_publish.py::test_new_generation_publishes_alongside_the_old`
- Violation smell: any PUT/copy targeting an existing published generation
  prefix.

### C2 — manifest written last

**Invariant:** The manifest MUST be written LAST as the completion marker;
death mid-promotion leaves a manifest-less prefix and a rerun redoes the
idempotent copies.
- Where: `library/src/shizzle_server/publish/publisher.py:34`, `docs/architecture.md:135`
- Guarded by: `library/tests/test_publish.py::test_promote_uses_server_side_copy_and_writes_manifest_last`, `library/tests/test_publish.py::test_manifest_absent_from_destination_until_every_object_copied`, `library/tests/test_publish.py::test_promote_refuses_a_staged_set_with_no_manifest`
- Violation smell: copying `manifest.json` before every other object, or
  download/re-upload instead of server-side copy.

### C3 — format guard before promotion

**Invariant:** The format guard MUST run before promotion: no raw PCM
(`.m4a` required for stems), `MAX_STEM_BYTES` 64 MiB per stem.
- Where: `library/src/shizzle_server/publish/publisher.py:174`, `library/src/shizzle_server/publish/publisher.py:178`, `library/src/shizzle_server/publish/publisher.py:605`
- Guarded by: `library/tests/test_publish.py::test_validate_stem_object_accepts_normal_m4a`, `library/tests/test_publish.py::test_validate_stem_object_rejects_wav`, `library/tests/test_publish.py::test_validate_stem_object_rejects_oversized_m4a`, `library/tests/test_publish.py::test_validate_stem_object_ignores_non_stem_objects`, `library/tests/test_publish.py::test_validate_stem_object_handles_unknown_size`, `library/tests/test_publish.py::test_validate_stem_objects_raises_with_every_problem_listed`, `library/tests/test_publish.py::test_publish_refuses_wav_stems_and_promotes_nothing`, `library/tests/test_publish.py::test_publish_refuses_oversized_stem`
- Violation smell: promoting a staged set before `validate_stem_objects`, or
  raising the byte cap.

### C4 — deterministic uuid5 track ids

**Invariant:** Track ids MUST be deterministic uuid5 values, so a crashed/rerun
publish or duplicate completion converges instead of double-publishing.
- Where: `library/src/shizzle_server/db/repository.py:36`, `library/src/shizzle_server/db/repository.py:88`, `library/src/shizzle_server/db/repository.py:574`
- Guarded by: `library/tests/test_publish.py::test_publish_job_writes_track_row_and_is_idempotent`, `library/tests/test_publish.py::test_track_id_for_import_is_deterministic_and_distinct_from_job_ids`, `library/tests/contract/test_orchestrator_postgres.py::test_concurrent_duplicate_completion_single_track`
- Violation smell: deriving a track id from a uuid4, timestamp, or job id.

### C5 — generation activation is compare-and-swap

**Invariant:** Generation activation MUST be a compare-and-swap under
`with_for_update`, with the ledger event committed in the SAME transaction.
- Where: `library/src/shizzle_server/db/repository.py:745`
- Guarded by: `library/tests/test_repository.py::test_generation_activation_is_compare_and_swap_with_append_only_event`, `library/tests/test_repository.py::test_generation_rollback_uses_same_atomic_ledger`
- Violation smell: flipping the active-generation pointer outside the CAS, or
  appending the ledger event in a second transaction.

### C6 — DB pointer flips only after a complete candidate

**Invariant:** The database pointer MUST flip only after the complete candidate
passes; a failed candidate stays outside the library; retry is idempotent.
- Where: `docs/architecture.md:137`
- Guarded by: `library/tests/test_lossless_intake.py::test_candidate_audit_fails_closed_on_release_blocking_issue`
- Violation smell: writing a library row before the candidate audit passes.

## D. Delivery-profile gates

### D1 — track duration tolerance 0.100 s

**Invariant:** `TRACK_DURATION_TOLERANCE_SEC` MUST be 0.100 (at least one full
30 fps frame of headroom; 0.050 tripped on H.264 GOP quantization).
derive_video's post-encode probe and the profile stream check are the SAME
invariant via the shared constant.
- Where: `library/src/shizzle_server/publish/delivery_profile.py:51`, consumers `library/src/shizzle_server/publish/lossless_intake.py:324`, `library/src/shizzle_server/publish/delivery_profile.py:288`
- Guarded by: `library/tests/test_lossless_intake.py::test_track_duration_tolerance_covers_h264_frame_quantization`, `library/tests/test_lossless_intake.py::test_derive_video_tolerates_clean_frame_quantization`
- Violation smell: a locally redefined tolerance at either consumer, or a
  literal `0.1`/`0.05` in a probe comparison.

### D2 — stem tolerances 0.080 / 0.005 s

**Invariant:** `STEM_DURATION_TOLERANCE_SEC` MUST be 0.080 (AAC
priming/padding headroom) while `STEM_INTER_DURATION_TOLERANCE_SEC` 0.005 is
the hard identical-timeline invariant between stems.
- Where: `library/src/shizzle_server/publish/delivery_profile.py:45`
- Guarded by: `library/tests/test_delivery_profile.py::test_passing_audio_probe`, `library/tests/test_delivery_profile.py::test_broken_legacy_video_shape_is_release_blocking`
- Violation smell: widening the inter-stem tolerance, or comparing stems to
  video with the inter-stem tolerance.

### D3 — one common attenuation, never a boost

**Invariant:** There MUST be at most ONE common attenuation across stems —
never per-stem, never a boost — with a true-peak ceiling of -1.0 dBTP.
- Where: `library/src/shizzle_server/publish/lossless_intake.py:56`, `docs/architecture.md:125`
- Guarded by: `library/tests/test_lossless_intake.py::test_common_gain_never_rounds_toward_zero`
- Violation smell: per-stem gain, or a gain above 1.0.

### D4 — browser generation ≤ 2.5 Mb/s average

**Invariant:** A complete browser generation MUST NOT exceed 2,500,000 b/s
average total bitrate.
- Where: `library/src/shizzle_server/publish/delivery_profile.py:58`, `library/src/shizzle_server/publish/lossless_intake.py:57`
- Guarded by: `library/tests/test_delivery_profile.py::test_existing_low_bitrate_is_warning_but_new_encode_is_error`
- Violation smell: raising the cap or exempting an artifact class from the
  average.

### D5 — new encodes AAC-LC 256k@44.1k, existing preserved

**Invariant:** New encodes MUST be AAC-LC 256 kbps at 44.1 kHz; existing
passing material is preserved, never lossily up-transcoded (warning, not
error).
- Where: `library/src/shizzle_server/publish/delivery_profile.py:21`, `library/src/shizzle_server/publish/delivery_profile.py:35`
- Guarded by: `library/tests/test_delivery_profile.py::test_existing_48khz_is_preserved_but_new_derivation_uses_44k1`, `library/tests/test_delivery_profile.py::test_compatible_source_frame_rates_and_high_profile_are_preserved`
- Violation smell: an error (instead of warning) on legacy-compatible
  material, or a new encode at 48 kHz.

### D6 — delivery video audio-less, bounded keyframes/start

**Invariant:** Delivery video MUST be audio-less, with
`VIDEO_MAX_KEYFRAME_INTERVAL_SEC` 2.05 and `START_TOLERANCE_SEC` 0.020.
- Where: `library/src/shizzle_server/publish/delivery_profile.py:43`, `library/src/shizzle_server/publish/delivery_profile.py:297`
- Guarded by: `library/tests/test_delivery_profile.py::test_passing_video_probe_matches_repaired_pot`, `library/tests/test_delivery_profile.py::test_broken_legacy_video_shape_is_release_blocking`
- Violation smell: an audio track in the delivery video, or a keyframe
  interval above 2.05 s.

### D7 — delivery_profile.py is pure policy

**Invariant:** `delivery_profile.py` MUST stay a PURE policy module — no
boto3/ffmpeg/db imports — and all consumers use the one profile.
- Where: `library/src/shizzle_server/publish/delivery_profile.py:1`
- Guarded by: `library/tests/test_delivery_profile.py::test_profile_manifest_block_is_versioned_and_unit_bearing`
- Violation smell: any new import of boto3, ffmpeg, or a database module in
  this file, or a consumer redefining profile constants locally.

## E. Credentials never persisted

### E1 — no credentials in telemetry

**Invariant:** Query credentials MUST NEVER appear in telemetry or durable
browser evidence — the validator rejects credential-shaped keys recursively
(authorization/cookie/password/passcode/token/url) and caps sizes.
- Where: `library/src/shizzle_server/api/models.py:118`, `library/src/shizzle_server/api/routes.py:345`
- Guarded by: `library/tests/test_playback_telemetry.py::test_telemetry_payload_rejects_credentials_and_unbounded_detail`
- Violation smell: widening the forbidden-key set's complement, raising the
  size caps, or logging raw query strings.

### E2 — source_ref never exposed in API responses

**Invariant:** `source_ref` (the raw submitted URL) is stored on the job row
but MUST NEVER be exposed in API responses (`JobResponse` omits it).
- Where: `library/src/shizzle_server/api/models.py:18`, `library/src/shizzle_server/db/models.py:99`
- UNGUARDED — no test asserts the omission; a wanted test should post a job
  with a credential-bearing URL and assert every response body lacks it.

### E3 — secrets only in env

**Invariant:** Secrets MUST live only in gitignored `.env` / the production
environment — never printed or committed — and worker images are published only
through the Actions workflow.
- Where: `docs/HANDOFF.md:119`, `deploy/vps/compose.prod.yml` (CloudFront key mounted read-only)
- Violation smell: a secret literal in any tracked file, log line, or
  hand-pushed image tag.

### E4 — passcode bound into every token signature

**Invariant:** The passcode MUST be bound into every token signature, so
rotating it revokes all tokens without a token store.
- Where: `library/src/shizzle_server/api/auth.py:3`, `library/src/shizzle_server/settings.py:63`
- Guarded by: `library/tests/test_auth_media.py`
- Violation smell: signing tokens over expiry alone.

### E5 — CloudFront URLs file-scoped under tracks/

**Invariant:** CloudFront signed URLs MUST be file-scoped and the signed key
MUST be under `tracks/`.
- Where: `library/src/shizzle_server/api/cloudfront.py:76`
- Guarded by: `library/tests/test_auth_media.py`
- Violation smell: a wildcard `tracks/*` cookie where a file-scoped URL
  suffices, or signing a key outside `tracks/`.

## F. Migration / DB conventions

### F1 — single linear migration chain

**Invariant:** Migrations MUST form a single linear chain with numeric prefix ==
revision id, explicit `down_revision`, and a paired real downgrade.
- Where: `library/alembic/versions/` — revisions 0001 through 0004
- Violation smell: a branch, a filename/revision mismatch, or a no-op
  downgrade.

### F2 — alembic DSN and metadata

**Invariant:** Alembic MUST take its DSN from `DATABASE_URL` and use
`target_metadata = Base.metadata`.
- Where: `library/alembic/env.py:17`
- Violation smell: a hardcoded URL or drift between models and metadata.

### F3 — single schema writer

**Invariant:** ONLY the explicit deploy transaction runs `alembic upgrade
head`, using the api image after a rollback snapshot and prior revision have
been recorded; the orchestrator and long-running api service never migrate.
- Where: `deploy/vps/deploy-release.sh`, `deploy/vps/restore-release.sh`
- Guarded by: `deploy/vps/tests/test_release_transaction.sh`
- Violation smell: a migration command in a long-running service entrypoint,
  or rollback that restores an old image without first downgrading the schema.

### F4 — the migration itself is under test

**Invariant:** The contract suite MUST apply the schema via a real
`alembic upgrade head` subprocess, never `create_all`.
- Where: `library/tests/contract/conftest.py:7`
- Guarded by: `library/tests/contract/test_orchestrator_postgres.py::test_migration_produced_expected_tables_and_constraints`
- Violation smell: `create_all` anywhere in test fixtures.

### F5 — job events append-only and ordered

**Invariant:** Job events MUST be append-only and ordered.
- Where: `library/src/shizzle_server/db/repository.py:194`
- Guarded by: `library/tests/test_repository.py::test_events_append_only_ordering`
- Violation smell: an UPDATE or DELETE on the events table.

### F6 — idempotency key unique per job

**Invariant:** The idempotency key MUST be unique per job.
- Guarded by: `library/tests/test_repository.py::test_idempotency_key_unique`
- Violation smell: relaxing the unique constraint or reusing a key across
  dispatches.
