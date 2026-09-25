# Recover a failed cloud publication

Use only after releasing the fix for the recorded publication failure. This
operation retains the original logical job, provider job and reservation. It
does not submit or cancel a RunPod job and does not retry separation.

Run inside the deployed API container, where settings, job data and the SDK
credential provider are already available:

```sh
python -m shizzle_server.orchestrator.recover_publication \
  --job-id JOB_UUID --provider-job-id PROVIDER_JOB_ID \
  --input-checksum SOURCE_SHA256 --recovery-id NEW_RECOVERY_UUID
```

The default command checks the exact failed publication, live provider
completion, existing lossless package bytes and actual source checksum. It
does not change job state or write objects. Missing local package/source or
unavailable provider history blocks recovery rather than permitting a new
submission. Investigate the specific boundary; do not reset the job to
`dispatched` or invent a replacement provider ID.

When RunPod's result retention has expired (HTTP 404), the explicit
`--use-completion-receipts` option can reconcile the durable `worker_completed`
event, selected attempt prefix, exact provider ID in the worker's dispatch
receipt, and handoff marker. The same package/source byte verification still
applies. Any mismatch blocks recovery. Authentication errors, outages and
non-completed provider statuses never use this fallback. Reports identify
whether live provider state or reconciled durable receipts supplied evidence.

After inspecting that report, repeat the same arguments with `--execute`.
The repository rechecks identity and latest failure under a row lock, refuses
any live lease or existing track, and acquires a bounded recovery lease at
`verifying`. The normal orchestrator loop renews that lease and runs existing
verification/publication handlers. Their atomic publication transaction
sets `ready`; the command never sets it directly.

On interruption, reconcile the job and the `publication_recovery_started`
event. An expired lease at `verifying`/`publishing` is reclaimed normally;
do not run this command against a nonterminal job. If publication fails
again, preserve the new failure evidence and address its cause before another
explicit recovery. The original failure and recovery events remain in the
append-only history. Rollback must preserve this database and must not replay
dispatch. This change has no schema migration.
