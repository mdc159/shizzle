"""Agent configuration, environment-driven (E3: no secrets in files).

Required:
    SHIZZLE_API_BASE        https://shizzle.systems
    SHIZZLE_NODE_ID         mac-mini | dell-9530 (must match SHIZZLE_NODE_TOKENS on the VPS)
    SHIZZLE_NODE_TOKEN      the node's bearer token
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (scoped IAM user, see node/README.md)

Optional:
    SHIZZLE_NODE_DISPLAY_NAME   "Mac mini"
    SHIZZLE_NODE_DEVICE         auto | mps | cuda | cpu   (default auto)
    SHIZZLE_NODE_CAPABILITIES   download,separate         (default both)
    SHIZZLE_NODE_WORKDIR        where sources and packages are staged
    SHIZZLE_NODE_HEARTBEAT_SECONDS   15
    SHIZZLE_NODE_POLL_WAIT_SECONDS   25   (long-poll length, server caps at 30)
    SHIZZLE_NODE_ACK_RETRY_SECONDS   5    (pause after an ack the API failed to answer)
    SHIZZLE_NODE_LOG_LEVEL      INFO (DEBUG | INFO | WARNING | ERROR)
    SHIZZLE_NODE_ALLOW_HTTP     1 to allow a plain-http SHIZZLE_API_BASE (local dev only)
    SHIZZLE_YTDLP_COOKIES       path to a Netscape cookies file for yt-dlp
    SHIZZLE_STEMSPLIT_DIR       path to the repo's stemsplit/ (default: ../../stemsplit)
    AWS_REGION                  us-east-1
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

_DEFAULT_STEMSPLIT = Path(__file__).resolve().parents[3] / "stemsplit"


class ConfigError(ValueError):
    pass


def _positive_float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number (got {raw!r})") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number (got {raw!r})")
    return value


def _flag_set(value: str) -> frozenset[str]:
    return frozenset(part.strip().lower() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class NodeConfig:
    api_base: str
    node_id: str
    token: str = field(repr=False)  # never in logs or tracebacks
    display_name: str
    device: str
    workdir: Path
    capabilities: frozenset[str]
    stemsplit_dir: Path
    heartbeat_seconds: float = 15.0
    poll_wait_seconds: int = 25
    # Pause after an ack the API answered with a non-409 error, before the
    # next poll offers the same open assignment again.
    ack_retry_seconds: float = 5.0
    cookies_file: Path | None = None
    aws_region: str = "us-east-1"
    agent_version: str = field(default=__version__)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> NodeConfig:
        env = dict(os.environ if env is None else env)

        def need(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigError(f"{name} is required")
            return value

        api_base = need("SHIZZLE_API_BASE").rstrip("/")
        if not api_base.startswith(("https://", "http://")):
            raise ConfigError("SHIZZLE_API_BASE must be an http(s) URL")
        if api_base.startswith("http://"):
            # Bearer tokens travel on every request: plain http only for a
            # local development control plane, and only when asked for.
            host = api_base[len("http://"):].split("/")[0].split(":")[0].lower()
            if host not in ("localhost", "127.0.0.1", "::1", "[::1]") and env.get(
                "SHIZZLE_NODE_ALLOW_HTTP", ""
            ).strip() != "1":
                raise ConfigError(
                    "SHIZZLE_API_BASE must be https:// (set SHIZZLE_NODE_ALLOW_HTTP=1 only for a "
                    "local development server)"
                )
        node_id = need("SHIZZLE_NODE_ID")
        if not all(ch.isalnum() or ch in "-_" for ch in node_id) or len(node_id) > 64:
            raise ConfigError("SHIZZLE_NODE_ID may only contain letters, digits, '-' and '_'")
        device = env.get("SHIZZLE_NODE_DEVICE", "auto").strip().lower() or "auto"
        if device not in ("auto", "mps", "cuda", "cpu"):
            raise ConfigError("SHIZZLE_NODE_DEVICE must be auto, mps, cuda or cpu")
        capabilities = _flag_set(env.get("SHIZZLE_NODE_CAPABILITIES", "download,separate"))
        unknown = capabilities - {"download", "separate"}
        if unknown:
            raise ConfigError(f"unknown capabilities: {', '.join(sorted(unknown))}")
        if not capabilities:
            raise ConfigError("SHIZZLE_NODE_CAPABILITIES must name download and/or separate")
        workdir = Path(env.get("SHIZZLE_NODE_WORKDIR") or (Path.home() / ".shizzle-node"))
        cookies = env.get("SHIZZLE_YTDLP_COOKIES", "").strip()
        stemsplit_dir = Path(env.get("SHIZZLE_STEMSPLIT_DIR") or _DEFAULT_STEMSPLIT)
        return cls(
            api_base=api_base,
            node_id=node_id,
            token=need("SHIZZLE_NODE_TOKEN"),
            display_name=env.get("SHIZZLE_NODE_DISPLAY_NAME", "").strip() or node_id,
            device=device,
            workdir=workdir,
            capabilities=capabilities,
            stemsplit_dir=stemsplit_dir,
            heartbeat_seconds=_positive_float(env, "SHIZZLE_NODE_HEARTBEAT_SECONDS", 15.0),
            poll_wait_seconds=max(1, min(30, int(_positive_float(env, "SHIZZLE_NODE_POLL_WAIT_SECONDS", 25.0)))),
            ack_retry_seconds=_positive_float(env, "SHIZZLE_NODE_ACK_RETRY_SECONDS", 5.0),
            cookies_file=Path(cookies) if cookies else None,
            aws_region=env.get("AWS_REGION", "").strip() or "us-east-1",
        )

    def capability_payload(self, *, resolved_device: str) -> dict[str, object]:
        return {
            "download": "download" in self.capabilities,
            "separate": "separate" in self.capabilities,
            "device": resolved_device,
            "agent": f"shizzle-node/{self.agent_version}",
        }
