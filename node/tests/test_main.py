"""The ``shizzle-node`` entry point: exit codes and the two-step stop."""

from __future__ import annotations

import httpx
import pytest

from shizzle_node.__main__ import main, signal_handler
from shizzle_node.agent import Agent
from shizzle_node.client import NodeClient
from shizzle_node.config import NodeConfig

GOOD = {"SHIZZLE_API_BASE": "https://shizzle.test", "SHIZZLE_NODE_ID": "mac-mini", "SHIZZLE_NODE_TOKEN": "tok"}


def test_main_rejects_a_bad_log_level_with_exit_2(capsys):
    assert main({**GOOD, "SHIZZLE_NODE_LOG_LEVEL": "LOUD"}, run=False) == 2
    assert "SHIZZLE_NODE_LOG_LEVEL" in capsys.readouterr().err


@pytest.mark.parametrize("broken", [{}, {**GOOD, "SHIZZLE_NODE_HEARTBEAT_SECONDS": "soon"},
                                    {**GOOD, "SHIZZLE_API_BASE": "http://evil.example"}])
def test_main_reports_config_errors_with_exit_2_not_a_traceback(capsys, broken):
    assert main(broken, run=False) == 2
    assert capsys.readouterr().err.startswith("shizzle-node: ")


def test_main_accepts_a_valid_configuration(capsys):
    assert main({**GOOD, "SHIZZLE_NODE_LOG_LEVEL": "debug"}, run=False) == 0
    assert capsys.readouterr().err == ""


def test_first_signal_drains_and_second_signal_exits_after_cleaning_up(tmp_path):
    config = NodeConfig.from_env({**GOOD, "SHIZZLE_NODE_WORKDIR": str(tmp_path / "work")})
    client = NodeClient("https://shizzle.test", "mac-mini", "tok",
                        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})))
    agent = Agent(config, client, s3_factory=lambda: None, device_resolver=lambda d: d)
    staged = tmp_path / "work" / "job" / "download"
    staged.mkdir(parents=True)
    (staged / "source.mp4").write_bytes(b"half")
    agent._workdir = staged  # a job is in flight
    exits: list[int] = []
    handler = signal_handler(agent, exit_now=exits.append)

    handler(15)
    assert agent.stop_requested and exits == [] and staged.exists()  # drain: finish the job first
    handler(15)
    assert exits == [130] and not staged.exists()  # now: the staged files are gone too
