"""Entrypoint: python -m shizzle_server.orchestrator"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from .loop import Orchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def _main() -> None:
    orch = Orchestrator()
    loop = asyncio.get_running_loop()
    for sig in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, sig, None)
        if signum is None:
            continue
        with contextlib.suppress(NotImplementedError):  # Windows event loop
            loop.add_signal_handler(signum, orch.request_stop)
    await orch.run_forever()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())
