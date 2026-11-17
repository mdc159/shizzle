"""Effective runtime settings: admin DB overrides over environment defaults.

The orchestrator resolves these at each routing decision (not once at
startup), so an owner change on the admin page takes effect on the next
pass without a restart. A missing (or invalid) stored row falls back to the
environment ``Settings`` value, so behaviour is unchanged until the owner
changes something.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..db.repository import RuntimeSettingsRepository

if TYPE_CHECKING:  # runtime import cycle with stages.py
    from .stages import StageContext

ROUTING_MODES = ("prefer", "only", "off", "download_only")
RUNPOD_MAX_CONCURRENT_KEY = "runpod_max_concurrent"
NODE_ROUTING_KEY = "node_routing"


def normalize_routing(value: Any) -> str | None:
    """The stored routing mode when it is one of the four legal values."""
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in ROUTING_MODES:
            return mode
    return None


def parse_runpod_limit(value: Any) -> tuple[bool, int | None]:
    """``(valid, limit)`` for a stored runpod_max_concurrent value.

    JSON null is a valid "no limit" (same as the env default). Returns
    ``(False, None)`` for garbage (bools, negatives, non-ints) so the admin
    page can report it and the orchestrator can fall back to the env value.
    """
    if value is None:
        return True, None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return False, None
    return True, value


async def _stored(repo: RuntimeSettingsRepository | None, key: str) -> Any:
    if repo is None:
        return None
    row = await repo.get(key)
    return row.value if row is not None else None


async def effective_node_routing(ctx: StageContext) -> str:
    """Routing mode for this pass: DB override when valid, else the env value."""
    mode = normalize_routing(await _stored(ctx.runtime, NODE_ROUTING_KEY)) if ctx.runtime else None
    return mode if mode is not None else ctx.settings.node_routing


async def effective_runpod_max_concurrent(ctx: StageContext) -> int | None:
    """RunPod concurrency cap for this pass: DB override when valid, else env.

    None means no limit; 0 pauses fresh RunPod dispatch. An invalid stored
    value falls back to the environment default.
    """
    if ctx.runtime is not None:
        valid, limit = parse_runpod_limit(
            await _stored(ctx.runtime, RUNPOD_MAX_CONCURRENT_KEY)
        )
        if valid:
            return limit
    return ctx.settings.runpod_max_concurrent


def _routing_problem(value: Any) -> str | None:
    if value is None:
        return None
    if normalize_routing(value) is None:
        return (
            f"node_routing must be one of {', '.join(ROUTING_MODES)} "
            f"(or null to clear the override)"
        )
    return None


def _limit_problem(value: Any) -> str | None:
    if value is None:
        return None
    valid, _ = parse_runpod_limit(value)
    if not valid:
        return "runpod_max_concurrent must be an integer >= 0 (or null for no limit)"
    return None


def validate_setting(key: str, value: Any) -> str | None:
    """Why ``value`` cannot be stored under ``key``; None when it can."""
    if key == NODE_ROUTING_KEY:
        return _routing_problem(value)
    if key == RUNPOD_MAX_CONCURRENT_KEY:
        return _limit_problem(value)
    return f"unknown setting {key!r}"


__all__ = [
    "NODE_ROUTING_KEY",
    "ROUTING_MODES",
    "RUNPOD_MAX_CONCURRENT_KEY",
    "effective_node_routing",
    "effective_runpod_max_concurrent",
    "normalize_routing",
    "parse_runpod_limit",
    "validate_setting",
]
