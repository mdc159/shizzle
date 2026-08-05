"""Command-contract tests for browser delivery encodes."""

from pathlib import Path

import audio_processing


def test_video_encode_matches_browser_profile(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(audio_processing, "_run", lambda cmd: commands.append(cmd))

    audio_processing.extract_video_only(tmp_path / "source.mp4", tmp_path / "video.mp4")

    cmd = commands[0]
    assert "-an" in cmd
    for args in (
        ["-c:v", "libx264"],
        ["-profile:v", "main"],
        ["-level:v", "3.1"],
        ["-pix_fmt", "yuv420p"],
        ["-vf", "setpts=PTS-STARTPTS"],
        ["-r", "30"],
        ["-g", "60"],
        ["-keyint_min", "60"],
        ["-sc_threshold", "0"],
        ["-movflags", "+faststart"],
        ["-video_track_timescale", "90000"],
    ):
        index = cmd.index(args[0])
        assert cmd[index:index + 2] == args


def test_stem_encode_pins_aac_lc_rate_and_channels(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(audio_processing, "_run", lambda cmd: commands.append(cmd))
    stems = {name: Path(f"{name}.wav") for name in audio_processing.STEM_ORDER}

    audio_processing.encode_aac_stems(stems, tmp_path / "stems", "256k")

    assert len(commands) == 6
    for cmd in commands:
        assert cmd[cmd.index("-profile:a") + 1] == "aac_low"
        assert cmd[cmd.index("-ar") + 1] == "44100"
        assert cmd[cmd.index("-ac") + 1] == "2"
        assert cmd[cmd.index("-b:a") + 1] == "256k"
