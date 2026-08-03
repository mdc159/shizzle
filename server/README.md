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
