# shizzle-server

FastAPI control plane for Shizzle: ingest endpoints, job/library API, and (from
Phase 2) the durable Postgres-backed orchestrator.

Trunk salvaged from `k25-nextgen-rewrite/local-server` (see
`../docs/provenance.md`). Tooling:

```
uv run --directory server ruff check .
uv run --directory server pytest -q
uv run --directory server mypy .
```

## Phase 2 layout

- `src/shizzle_server/db/` — SQLAlchemy 2.x models (`jobs`, append-only
  `job_events`, `tracks`, `orchestrator_heartbeats`) + typed repository layer
  (the only place SQL happens).
- `src/shizzle_server/orchestrator/` — durable lease loop
  (`SELECT … FOR UPDATE SKIP LOCKED`, ~120 s leases with heartbeat renewal,
  exponential backoff via `next_retry_at`, idempotent stage handlers).
  Run standalone: `python -m shizzle_server.orchestrator`. The single-container
  `local` GPU profile instead runs it embedded in the API process
  (`SHIZZLE_EMBEDDED_ORCHESTRATOR=true`, SQLite fallback).
- `alembic/` — schema authority for Postgres (`alembic upgrade head`;
  the stack api container runs it on start). SQLite (local profile) uses
  `create_all` and is a throwaway cache, not a migrated store.

## Tests

- Unit suite (SQLite, fast): `uv run --directory server pytest -q -m "not postgres"`
- Contract/fault-injection suite (REAL Postgres, no mocked DB):

```
docker compose -p shizzle-test -f infra/compose.yml --profile test up -d postgres
uv run --directory server pytest tests/contract -q
```

Covers: hard-kill mid-stage + restart resumes exactly once (effect counters),
expired-lease reclaim by a second instance, concurrent duplicate completion,
retry schedule honored, two live orchestrators with SKIP LOCKED, structured
`YTDLP_BLOCKED` stub failure, schema constraints.
