"""``shizzle-node``: run the worker-node agent until SIGTERM/SIGINT."""

from __future__ import annotations

import logging
import os
import signal
import sys

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


def main() -> int:
    level = os.environ.get("SHIZZLE_NODE_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        print(f"shizzle-node: SHIZZLE_NODE_LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR (got {level!r})",
              file=sys.stderr)
        return 2
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = NodeConfig.from_env()
    except (ConfigError, ValueError) as exc:
        print(f"shizzle-node: {exc}", file=sys.stderr)
        return 2
    client = NodeClient(config.api_base, config.node_id, config.token)
    agent = Agent(config, client, s3_factory=_s3_factory(config.aws_region))

    def _stop(signum, _frame):  # noqa: ARG001
        log = logging.getLogger("shizzle_node")
        if agent.stop_requested:
            # Second signal while a job runs: the operator means now. The
            # assignment is left to the orchestrator's stall watchdog (B16).
            log.warning("signal %s again: exiting now, abandoning the current job", signum)
            os._exit(130)
        log.info("signal %s: stopping after the current job (send again to exit now)", signum)
        agent.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _stop)
    try:
        agent.run()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
