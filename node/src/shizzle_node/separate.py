"""Separation executor: run the production lossless handler on this node.

This imports ``stemsplit/lossless_handler.py`` from the repository checkout
(the same file the RunPod image copies) and calls ``handler()`` with the
assignment payload. The handler downloads the source from S3, claims the
attempt prefix, separates, uploads six WAVs and writes ``handoff.json`` last.
The only node-specific inputs are the executor id and the device.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

Progress = Callable[[str], None]


def resolve_device(requested: str) -> str:
    """``auto`` -> mps on Apple silicon, cuda when available, else cpu."""
    if requested != "auto":
        return requested
    # A missing torch is a broken install, not "cpu": let the ImportError end
    # the agent at startup instead of advertising a separator that cannot run.
    import torch

    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def import_handler(stemsplit_dir: Path) -> Any:
    """Import the repo's lossless handler as a top-level module (flat layout)."""
    if not (stemsplit_dir / "lossless_handler.py").is_file():
        raise FileNotFoundError(f"lossless_handler.py not found under {stemsplit_dir}")
    path = str(stemsplit_dir)
    if path not in sys.path:
        sys.path.insert(0, path)
    import lossless_handler

    return lossless_handler


def run_separation(
    payload: dict[str, Any],
    *,
    executor_job_id: str,
    device: str,
    worker_image: str,
    stemsplit_dir: Path,
    progress: Progress,
) -> dict[str, Any]:
    """Execute one separation assignment; returns the handler's result dict."""
    os.environ["WORKER_IMAGE"] = worker_image
    module = import_handler(stemsplit_dir)
    module.WORKER_IMAGE = worker_image
    job = {"id": executor_job_id, "input": {**payload, "device": device}}
    result = module.handler(job, progress=progress)
    if not isinstance(result, dict):
        raise RuntimeError(f"lossless_handler.handler returned {type(result).__name__}, expected dict")
    return dict(result)
