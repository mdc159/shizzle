import sys
from types import ModuleType

import numpy as np

from lossless_worker import ROLES, _separation_callback, separate


def test_separation_callback_deduplicates_percent_and_never_raises():
    messages = []
    callback = _separation_callback(messages.append)

    callback({"segment_offset": 25, "audio_length": 100, "state": "start"})
    callback({"segment_offset": 25, "audio_length": 100, "state": "end"})
    callback({"segment_offset": 500, "audio_length": 100})
    callback({"segment_offset": "bad", "audio_length": 100})
    callback(None)

    assert messages == ["separate: 25%", "separate: 100%"]

    def broken_heartbeat(_message):
        raise RuntimeError("watchdog unavailable")

    _separation_callback(broken_heartbeat)(
        {"segment_offset": 1, "audio_length": 2}
    )


def test_separate_emits_100_percent_after_demucs_returns(monkeypatch, tmp_path):
    returned = False
    messages = []

    class Tensor:
        def cpu(self):
            return self

        def numpy(self):
            return np.zeros((2, 8), dtype=np.float32)

    class Separator:
        samplerate = 44100

        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def separate_audio_file(self, _path):
            nonlocal returned
            self.callback({"segment_offset": 4, "audio_length": 10, "state": "start"})
            returned = True
            return None, {
                "other" if role == "shizzle" else role: Tensor() for role in ROLES
            }

    torch = ModuleType("torch")
    torch.cuda = type("Cuda", (), {"is_available": staticmethod(lambda: False)})()
    demucs = ModuleType("demucs")
    demucs.__version__ = "test"
    demucs_api = ModuleType("demucs.api")
    demucs_api.Separator = Separator
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "demucs", demucs)
    monkeypatch.setitem(sys.modules, "demucs.api", demucs_api)

    def heartbeat(message):
        if message == "separate: 100%":
            assert returned
        messages.append(message)

    separate(tmp_path / "audio.wav", heartbeat=heartbeat)
    assert messages[-1] == "separate: 100%"
