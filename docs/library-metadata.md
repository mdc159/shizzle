# Library metadata maintenance

Use [`ops/normalize_track_metadata.py`](../ops/normalize_track_metadata.py) to
review and apply explicit title/artist changes or soft deletions. The script
does not change S3 media, active generations or manifests.

## Mapping and validation

A mapping uses schema `shizzle-track-metadata-fix-v1`. Each entry identifies a
track by a full UUID plus expected values, or an unambiguous eight-character
ID prefix. Updates provide target title and artist; delete entries use
`action: delete` and require expected values or an explanatory note.

The script requires coverage of every nondeleted track, rejects duplicate or
ambiguous targets, and validates expected values before writing. Without
`--apply` it prints a dry-run plan only. With `--apply`, it locks the Postgres
tracks table, revalidates the plan and coverage, and applies updates/deletions
in one transaction. Soft deletion retains media and generation history.

The committed mapping and run JSON under `ops/data/` are the completed
production run's mapping and record, not test fixtures. Prepare a new mapping from current data for new work; do not
reuse its old expected values as a current inventory.

## Local invocation

Set `DATABASE_URL` securely in the process environment. From the repository:

```powershell
uv run --directory library python ../ops/normalize_track_metadata.py --mapping <new-mapping.json>
```

Review the dry-run table and any violations. For an intentional approved apply,
pass `--apply --report <new-report.json>`. Avoid putting credential-bearing
DSNs on command lines. Retain the report, mapping and their hashes.

## Running inside the production API image

The release ships installed application code, not a repository checkout.
Transfer the current script and reviewed mapping into an operator directory
on the VPS. Mount that directory into a one-off API container and use its
existing database configuration:

```bash
cd /opt/shizzle/prod
docker compose -p shizzle -f compose.prod.yml run --rm --no-deps \
  -v /opt/shizzle/prod/ops-normalize:/tmp/ops:rw \
  api python /tmp/ops/normalize_track_metadata.py --mapping /tmp/ops/mapping.json
```

To perform the reviewed changes, repeat with
`--apply --report /tmp/ops/new-run.json`. This is a state-changing maintenance
operation, not a startup step. The output path is checked before database writes.

Exit 2 reports validation/configuration problems; inspect the actual message
rather than treating every failure as “already applied.” A post-commit report
write failure prints the complete run record for recovery. Do not blindly rerun
an apply after such a failure; first verify the database and save the record.

Do not rerun the old importer to refresh display metadata: it can reset active
generations and restore deleted tracks. [Review](REVIEW.md) links the executable
guard issue.
