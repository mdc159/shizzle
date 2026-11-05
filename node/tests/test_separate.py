"""Separation executor pieces: device resolution, handler import, the call contract."""

from __future__ import annotations

import sys
import types

import pytest

from shizzle_node import separate


def _fake_torch(*, mps: bool, cuda: bool) -> types.ModuleType:
    torch = types.ModuleType("torch")
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps))  # type: ignore[attr-defined]
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)  # type: ignore[attr-defined]
    return torch


@pytest.mark.parametrize(
    ("requested", "mps", "cuda", "expected"),
    [("mps", False, False, "mps"), ("cpu", True, True, "cpu"),
     ("auto", True, False, "mps"), ("auto", False, True, "cuda"), ("auto", False, False, "cpu")],
)
def test_resolve_device_honours_an_explicit_choice_and_probes_for_auto(
    monkeypatch, requested, mps, cuda, expected
):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(mps=mps, cuda=cuda))
    assert separate.resolve_device(requested) == expected


def test_resolve_device_auto_fails_loudly_without_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch -> ImportError
    with pytest.raises(ImportError):
        separate.resolve_device("auto")


def test_import_handler_requires_the_repo_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="lossless_handler.py not found"):
        separate.import_handler(tmp_path)


def test_run_separation_calls_the_handler_with_executor_id_device_and_image(tmp_path, monkeypatch):
    stemsplit = tmp_path / "stemsplit"
    stemsplit.mkdir()
    (stemsplit / "lossless_handler.py").write_text(
        "WORKER_IMAGE = 'unset'\n"
        "CALLS = []\n"
        "def handler(job, *, progress=None):\n"
        "    progress('separating')\n"
        "    CALLS.append(job)\n"
        "    return {'status': 'ok', 'image': WORKER_IMAGE}\n"
    )
    monkeypatch.delitem(sys.modules, "lossless_handler", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))  # import_handler's insert is undone
    monkeypatch.setenv("WORKER_IMAGE", "")
    phases: list[str] = []
    result = separate.run_separation(
        {"track_id": "t", "device": "cpu"},
        executor_job_id="node:abc",
        device="mps",
        worker_image="node:mac/shizzle-node-0.1/mps",
        stemsplit_dir=stemsplit,
        progress=phases.append,
    )
    module = sys.modules["lossless_handler"]
    job = module.CALLS[0]
    assert job["id"] == "node:abc"
    assert job["input"]["device"] == "mps"  # the agent's device wins over the payload
    assert result == {"status": "ok", "image": "node:mac/shizzle-node-0.1/mps"}
    assert phases == ["separating"]
    assert str(stemsplit) in sys.path  # restored by monkeypatch after the test


def test_run_separation_rejects_a_non_dict_result(tmp_path, monkeypatch):
    stemsplit = tmp_path / "stemsplit"
    stemsplit.mkdir()
    (stemsplit / "lossless_handler.py").write_text(
        "WORKER_IMAGE = ''\n"
        "def handler(job, *, progress=None):\n"
        "    return 'done'\n"
    )
    monkeypatch.delitem(sys.modules, "lossless_handler", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("WORKER_IMAGE", "")
    with pytest.raises(RuntimeError, match="expected dict"):
        separate.run_separation(
            {}, executor_job_id="x", device="cpu", worker_image="i",
            stemsplit_dir=stemsplit, progress=lambda _m: None,
        )
