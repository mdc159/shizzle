from lossless_worker import _separation_callback


def test_separation_callback_reports_percent_and_never_raises():
    messages = []
    callback = _separation_callback(messages.append)

    callback({"segment_offset": 25, "audio_length": 100, "state": "start"})
    callback({"segment_offset": 500, "audio_length": 100})
    callback({"segment_offset": "bad", "audio_length": 100})
    callback(None)

    assert messages == ["separate: 25%", "separate: 100%"]

    def broken_heartbeat(_message):
        raise RuntimeError("watchdog unavailable")

    _separation_callback(broken_heartbeat)(
        {"segment_offset": 1, "audio_length": 2}
    )
