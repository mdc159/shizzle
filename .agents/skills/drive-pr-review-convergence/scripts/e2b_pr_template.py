#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "e2b==2.35.0",
# ]
# ///
"""Build the versioned E2B image used by the PR convergence controller."""

from __future__ import annotations

import argparse
import os
import sys

from e2b import Template

TIERS = {
    "small": (2, 4096),
    "standard": (4, 8192),
    "heavy": (8, 16384),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias", default="pr-review-v1")
    parser.add_argument("--tier", choices=sorted(TIERS), default="standard")
    args = parser.parse_args()
    if not os.environ.get("E2B_API_KEY"):
        parser.error("E2B_API_KEY is not present in the host environment")

    cpu_count, memory_mb = TIERS[args.tier]
    template = (
        Template()
        .from_node_image("22")
        .apt_install(
            [
                "bash",
                "ca-certificates",
                "curl",
                "git",
                "jq",
                "openssh-client",
                "rsync",
                "shellcheck",
                "unzip",
            ]
        )
        .run_cmd(
            "curl -LsSf https://astral.sh/uv/install.sh | "
            "env UV_INSTALL_DIR=/usr/local/bin sh",
            user="root",
        )
        .run_cmd("git --version && uv --version && node --version && npm --version")
        .set_workdir("/workspace")
    )
    result = Template.build(
        template,
        alias=args.alias,
        cpu_count=cpu_count,
        memory_mb=memory_mb,
        tags=[
            "default",
            "pr-review-controller-managed",
            "pr-review-convergence",
            f"tier-{args.tier}",
        ],
    )
    print(f"built E2B template alias={args.alias} tier={args.tier}")
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
