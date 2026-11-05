"""``shizzle-node``: run the worker-node agent until SIGTERM/SIGINT."""

from __future__ import annotations

import logging
import os
import signal
import sys
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config

from .agent import Agent
from .client import NodeClient
from .config import ConfigError, NodeConfig


def _s3_factory(region: str):
    def make():
        # Machine gotcha (see library/.../api/media.py): a global AWS_ENDPOINT_URL
        # pointing at another S3 provider must never reach the real bucket.
        os.environ.pop("AWS_ENDPOINT_URL", None)
        os.environ.pop("AWS_ENDPOINT_URL_S3", None)
        return boto3.client(
            "s3", region_name=region, config=Config(ignore_configured_endpoint_urls=True)
        )

    return make


LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def signal_handler(agent: Agent, *, exit_now: Callable[[int], Any] = os._exit) -> Callable[..., None]:
    """First signal: finish the current job, then stop. Second: exit now."""

    def _stop(signum: int, _frame: object = None) -> None:
        log = logging.getLogger("shizzle_node")
        if agent.stop_requested:
            # Second signal while a job runs: the operator means now. The
            # staged files go first (os._exit skips the agent's finally); the
            # assignment is left to the orchestrator's stall watchdog (B16).
            log.warning("signal %s again: exiting now, abandoning the current job", signum)
            agent.abandon()
            exit_now(130)
            return
        log.info("signal %s: stopping after the current job (send again to exit now)", signum)
        agent.request_stop()

    return _stop


def main(env: dict[str, str] | None = None, *, run: bool = True) -> int:
    env = dict(os.environ if env is None else env)
    level = env.get("SHIZZLE_NODE_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if level not in LOG_LEVELS:
        print(
            f"shizzle-node: SHIZZLE_NODE_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)} (got {level!r})",
            file=sys.stderr,
        )
        return 2
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = NodeConfig.from_env(env)
    except (ConfigError, ValueError) as exc:
        print(f"shizzle-node: {exc}", file=sys.stderr)
        return 2
    if not run:  # configuration check only
        return 0
    client = NodeClient(config.api_base, config.node_id, config.token)
    agent = Agent(config, client, s3_factory=_s3_factory(config.aws_region))
    handler = signal_handler(agent)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)
    try:
        agent.run()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
