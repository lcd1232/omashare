"""Screen capture: gpu-screen-recorder → ffmpeg → Annex-B H.264 on stdout.

The mirror session runs ``video_command`` through a shell and SIGKILLs it on
stop, which would orphan a plain ``gsr | ffmpeg`` pipeline. So the command is
``exec python -m omashare.capture ...``: this module becomes that process,
starts the pipeline with PR_SET_PDEATHSIG, and the children die with it.
SIGKILL, not SIGTERM: on SIGTERM both tools try to flush a trailer into a
pipe nobody reads any more and hang; a live stream has nothing to finalize.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass

PR_SET_PDEATHSIG = 1


@dataclass(frozen=True)
class Monitor:
    name: str
    width: int
    height: int


def focused_monitor() -> Monitor:
    """The focused Hyprland monitor, in physical pixels as captured."""
    out = subprocess.run(
        ["hyprctl", "monitors", "-j"], check=True, capture_output=True, text=True
    ).stdout
    monitors = json.loads(out)
    mon = next((m for m in monitors if m.get("focused")), monitors[0])
    width, height = int(mon["width"]), int(mon["height"])
    # Odd transforms (1, 3, 5, 7) rotate by 90°.
    if int(mon.get("transform", 0)) % 2:
        width, height = height, width
    return Monitor(mon["name"], width, height)


def fit_size(width: int, height: int, max_w: int, max_h: int) -> tuple[int, int]:
    """Scale (width, height) to fit within max_w x max_h, keeping aspect.

    Matches gpu-screen-recorder's ``-s`` behaviour (never upscales, even
    dimensions), so the size advertised to the receiver is the encoded one.
    """
    scale = min(max_w / width, max_h / height, 1.0)
    return int(width * scale) & ~1, int(height * scale) & ~1


def pipeline_argv(
    monitor: str, fps: int, max_w: int, max_h: int, cursor: bool
) -> tuple[list[str], list[str]]:
    """The gpu-screen-recorder and ffmpeg argv of the capture pipeline."""
    gsr = [
        "gpu-screen-recorder",
        "-w", monitor,
        "-c", "mpegts",
        "-k", "h264",
        "-f", str(fps),
        "-fm", "cfr",
        "-s", f"{max_w}x{max_h}",
        # Short GOP: the receiver can only start decoding at an IDR.
        "-keyint", "1",
        "-cursor", "yes" if cursor else "no",
        "-o", "/dev/stdout",
    ]  # fmt: skip
    ffmpeg = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer", "-probesize", "32768", "-analyzeduration", "0",
        "-f", "mpegts", "-i", "-",
        "-map", "0:v", "-c:v", "copy", "-bsf:v", "h264_mp4toannexb",
        "-flush_packets", "1", "-f", "h264", "-",
    ]  # fmt: skip
    return gsr, ffmpeg


def video_command(monitor: str, fps: int, max_w: int, max_h: int, cursor: bool) -> str:
    """Shell command for MirrorSession's ``video_command``."""
    args = [
        sys.executable, "-m", "omashare.capture",
        "--monitor", monitor, "--fps", str(fps),
        "--max-size", f"{max_w}x{max_h}",
    ]  # fmt: skip
    if not cursor:
        args.append("--no-cursor")
    return "exec " + shlex.join(args)


def _die_with_parent() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omashare.capture")
    parser.add_argument("--monitor", required=True)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-size", default="1920x1080")
    parser.add_argument("--no-cursor", action="store_true")
    args = parser.parse_args(argv)
    max_w, max_h = (int(v) for v in args.max_size.split("x"))

    _die_with_parent()
    gsr_argv, ffmpeg_argv = pipeline_argv(
        args.monitor, args.fps, max_w, max_h, not args.no_cursor
    )
    gsr = subprocess.Popen(
        gsr_argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        preexec_fn=_die_with_parent,
    )
    ffmpeg = subprocess.Popen(
        ffmpeg_argv, stdin=gsr.stdout, preexec_fn=_die_with_parent
    )
    assert gsr.stdout is not None
    gsr.stdout.close()  # ffmpeg owns the read end now

    def _stop(*_):
        for proc in (gsr, ffmpeg):
            if proc.poll() is None:
                proc.kill()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    code = ffmpeg.wait()
    _stop()
    gsr.wait()
    return code


if __name__ == "__main__":
    sys.exit(main())
