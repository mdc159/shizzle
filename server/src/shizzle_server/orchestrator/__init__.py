"""Durable Postgres-backed orchestrator (design spec section 4).

Run standalone:  python -m shizzle_server.orchestrator
Embedded mode:   the API process starts the same loop as an asyncio task when
                 SHIZZLE_EMBEDDED_ORCHESTRATOR=true (single-container `local`
                 profile with the SQLite fallback).
"""

from .loop import Orchestrator

__all__ = ["Orchestrator"]
