import asyncio
import shlex
import subprocess
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
    assert argv[argv.index("--target") + 1] == "HDMI-A-1"
    assert "--portal-token" not in argv
    assert "--no-cursor" in argv


def test_focused_monitor_swaps_rotated_dimensions(monkeypatch):
    out = '[{"name":"DP-1","width":2560,"height":1440,"transform":1,"focused":true}]'

    class Result:
        stdout = out

    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: Result())
    assert capture.focused_monitor() == capture.Monitor("DP-1", 1440, 2560)


def test_portal_pipeline_restores_from_the_token_file(tmp_path):
    token = tmp_path / "token"
    gsr, _ = capture.pipeline_argv(capture.PORTAL, 30, 1920, 1080, True, token)
    assert gsr[gsr.index("-w") + 1] == "portal"
    assert gsr[gsr.index("-restore-portal-session") + 1] == "yes"
    assert gsr[gsr.index("-portal-session-token-filepath") + 1] == str(token)


def test_portal_pipeline_needs_a_token_file():
    with pytest.raises(ValueError):
        capture.pipeline_argv(capture.PORTAL, 30, 1920, 1080, True)


def test_video_command_passes_the_portal_token(tmp_path):
    cmd = capture.video_command(capture.PORTAL, 30, 1920, 1080, True, tmp_path / "t")
    argv = shlex.split(cmd[len("exec ") :])
    assert argv[argv.index("--target") + 1] == "portal"
    assert argv[argv.index("--portal-token") + 1] == str(tmp_path / "t")


def _h264(tmp_path, size):
    path = tmp_path / f"{size}.h264"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30",
         "-t", "0.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "h264", str(path)],
        check=True,
    )  # fmt: skip
    return path


def test_pick_source_reads_the_size_from_the_first_sps(tmp_path, monkeypatch):
    stream = _h264(tmp_path, "1366x768")
    token = tmp_path / "token"
    token.write_text("stale")
    monkeypatch.setattr(capture, "capture_argv", lambda *a: ["cat", str(stream)])
    assert asyncio.run(capture.pick_source(token, 30, 1920, 1080, True)) == (1366, 768)
    # The stale token was dropped so the dialog shows again.
    assert not token.exists()


def _fake_capture(stderr: str, code: int):
    return [sys.executable, "-c", f"import sys; sys.stderr.write({stderr!r}); sys.exit({code})"]


def test_pick_source_raises_cancelled_when_the_portal_declines(tmp_path, monkeypatch):
    msg = "gsr info: ...\ngsr error: gsr_capture_portal_setup_dbus: Start failed\n"
    monkeypatch.setattr(capture, "capture_argv", lambda *a: _fake_capture(msg, 1))
    with pytest.raises(capture.PickCancelled):
        asyncio.run(capture.pick_source(tmp_path / "token", 30, 1920, 1080, True))


def test_pick_source_reports_other_failures_as_errors(tmp_path, monkeypatch):
    msg = "omashare: gpu-screen-recorder is not installed\n"
    monkeypatch.setattr(capture, "capture_argv", lambda *a: _fake_capture(msg, 127))
    with pytest.raises(capture.CaptureError, match="not installed"):
        asyncio.run(capture.pick_source(tmp_path / "token", 30, 1920, 1080, True))


def test_capture_reports_a_missing_recorder(monkeypatch, capsys):
    monkeypatch.setattr(capture, "pipeline_argv", lambda *a: (["no-such-gsr-binary"], ["cat"]))
    monkeypatch.setattr(capture, "_die_with_parent", lambda: None)
    assert capture.main(["--target", "eDP-1"]) == 127
    assert "not installed" in capsys.readouterr().err


def test_a_named_monitor_reports_its_own_size(monkeypatch):
    out = (
        '[{"name":"eDP-1","width":2880,"height":1920,"focused":true},'
        '{"name":"DP-1","width":3840,"height":2160,"focused":false}]'
    )

    class Result:
        stdout = out

    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: Result())
    assert capture.focused_monitor("DP-1") == capture.Monitor("DP-1", 3840, 2160)
    with pytest.raises(LookupError, match="no monitor called HDMI-A-1"):
        capture.focused_monitor("HDMI-A-1")
