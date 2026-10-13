import shlex
import sys

import pytest

from omashare import capture


@pytest.mark.parametrize(
    ("size", "limit", "expected"),
    [
        # 3:2 laptop panel into 1080p: height-bound, matches what gsr encodes.
        ((2880, 1920), (1920, 1080), (1620, 1080)),
        ((3840, 2160), (1920, 1080), (1920, 1080)),
        # Never upscale.
        ((1280, 720), (1920, 1080), (1280, 720)),
        # Odd results round down to even.
        ((2561, 1441), (1281, 721), (1280, 720)),
    ],
)
def test_fit_size(size, limit, expected):
    assert capture.fit_size(*size, *limit) == expected


def test_pipeline_emits_annexb_h264_on_stdout():
    gsr, ffmpeg = capture.pipeline_argv("DP-1", 60, 1280, 720, cursor=False)
    assert gsr[gsr.index("-w") + 1] == "DP-1"
    assert gsr[gsr.index("-f") + 1] == "60"
    assert gsr[gsr.index("-s") + 1] == "1280x720"
    assert gsr[gsr.index("-cursor") + 1] == "no"
    assert gsr[gsr.index("-k") + 1] == "h264"
    assert ffmpeg[-3:] == ["-f", "h264", "-"]
    assert "h264_mp4toannexb" in ffmpeg


def test_video_command_execs_this_interpreter():
    cmd = capture.video_command("HDMI-A-1", 30, 1920, 1080, cursor=False)
    assert cmd.startswith("exec ")
    argv = shlex.split(cmd[len("exec ") :])
    assert argv[:3] == [sys.executable, "-m", "omashare.capture"]
    assert argv[argv.index("--monitor") + 1] == "HDMI-A-1"
    assert "--no-cursor" in argv


def test_focused_monitor_swaps_rotated_dimensions(monkeypatch):
    out = '[{"name":"DP-1","width":2560,"height":1440,"transform":1,"focused":true}]'

    class Result:
        stdout = out

    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: Result())
    assert capture.focused_monitor() == capture.Monitor("DP-1", 1440, 2560)
