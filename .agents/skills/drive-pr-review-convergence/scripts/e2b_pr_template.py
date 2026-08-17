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
UV_VERSION = "0.12.5"
UV_ARCHIVE_SHA256 = "68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2"


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
            "set -euo pipefail; "
            f"archive=/tmp/uv-{UV_VERSION}.tar.gz; "
            f"curl -LsSf -o $archive https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz; "
            f"echo '{UV_ARCHIVE_SHA256}  '$archive | sha256sum -c -; "
            "tar -xzf $archive -C /tmp; "
            "install -m 0755 /tmp/uv-x86_64-unknown-linux-gnu/uv /usr/local/bin/uv; "
            "install -m 0755 /tmp/uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/uvx; "
            "rm -rf $archive /tmp/uv-x86_64-unknown-linux-gnu",
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
