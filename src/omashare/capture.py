"""Screen capture: gpu-screen-recorder → ffmpeg → Annex-B H.264 on stdout.

The mirror session runs ``video_command`` through a shell and SIGKILLs it on
stop, which would orphan a plain ``gsr | ffmpeg`` pipeline. So the command is
``exec python -m omashare.capture ...``: this module becomes that process,
starts the pipeline with PR_SET_PDEATHSIG, and the children die with it.
SIGKILL, not SIGTERM: on SIGTERM both tools try to flush a trailer into a
pipe nobody reads any more and hang; a live stream has nothing to finalize.

The source is a monitor, or whatever was picked in the desktop's screen-share
dialog (xdg-desktop-portal; on Omarchy, hyprland-preview-share-picker).
:func:`pick_source` shows that dialog and learns the picked source's size;
the portal restore token it leaves behind lets the session's own capture
reopen the same source without asking again.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from pyatv.protocols.airplay.mirror import pacer

from omashare.h264 import find_sps, sps_dimensions
from omashare.proc import die_with_parent


#: gpu-screen-recorder's capture target for the screen-share portal.
PORTAL = "portal"


class PickCancelled(Exception):
    """The screen-share dialog was closed without picking anything."""


class CaptureError(Exception):
    """The capture could not start (missing tool, encoder or portal error)."""


#: What gpu-screen-recorder prints when the portal declines to share, which
#: is what closing or cancelling the screen-share dialog does.
_PORTAL_DECLINED = ("SelectSources failed", "Start failed")


def _last_message(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    useful = [line for line in lines if not line.startswith("gsr info")]
    return (useful or lines or ["no output"])[-1]


@dataclass(frozen=True)
class Monitor:
    name: str
    width: int
    height: int


def focused_monitor(name: Optional[str] = None) -> Monitor:
    """The focused Hyprland monitor, or the one called *name*, in physical
    pixels as captured."""
    out = subprocess.run(
        ["hyprctl", "monitors", "-j"], check=True, capture_output=True, text=True
    ).stdout
    monitors = json.loads(out)
    if name:
        mon = next((m for m in monitors if m.get("name") == name), None)
        if mon is None:
            known = ", ".join(m.get("name", "?") for m in monitors)
            raise LookupError(f"no monitor called {name} (have: {known})")
    else:
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
    target: str,
    fps: int,
    max_w: int,
    max_h: int,
    cursor: bool,
    portal_token: Optional[Path] = None,
) -> tuple[list[str], list[str]]:
    """The gpu-screen-recorder and ffmpeg argv of the capture pipeline.

    *target* is a monitor name or :data:`PORTAL`. For the portal, a restore
    token in *portal_token* reopens the source picked last time; without one
    the screen-share dialog is shown and the new token is saved there.
    """
    portal_args: list[str] = []
    if target == PORTAL:
        if portal_token is None:
            raise ValueError("portal capture needs a token file")
        portal_args = [
            "-restore-portal-session", "yes",
            "-portal-session-token-filepath", str(portal_token),
        ]  # fmt: skip
    gsr = [
        "gpu-screen-recorder",
        "-w", target,
        *portal_args,
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


def capture_argv(
    target: str,
    fps: int,
    max_w: int,
    max_h: int,
    cursor: bool,
    portal_token: Optional[Path] = None,
) -> list[str]:
    """argv that runs this module as the capture process."""
    args = [
        sys.executable, "-m", "omashare.capture",
        "--target", target, "--fps", str(fps),
        "--max-size", f"{max_w}x{max_h}",
    ]  # fmt: skip
    if portal_token is not None:
        args += ["--portal-token", str(portal_token)]
    if not cursor:
        args.append("--no-cursor")
    return args


def video_command(
    target: str,
    fps: int,
    max_w: int,
    max_h: int,
    cursor: bool,
    portal_token: Optional[Path] = None,
) -> str:
    """Shell command for MirrorSession's ``video_command``."""
    return "exec " + shlex.join(
        capture_argv(target, fps, max_w, max_h, cursor, portal_token)
    )


async def pick_source(
    portal_token: Path, fps: int, max_w: int, max_h: int, cursor: bool
) -> tuple[int, int]:
    """Show the screen-share dialog and return the picked source's size.

    The size is the encoded one (already fitted into max_w x max_h), read
    from the first SPS. Leaves a restore token in *portal_token* for the
    session's capture. Raises :class:`PickCancelled` if nothing was picked
    and :class:`CaptureError` if the capture failed for any other reason.
    """
    # No token: the dialog is shown every time, not just the first time.
    portal_token.unlink(missing_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *capture_argv(PORTAL, fps, max_w, max_h, cursor, portal_token),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    # Drained alongside stdout, so a chatty stderr can't fill its pipe.
    stderr_task = asyncio.ensure_future(proc.stderr.read())
    buf = b""
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                code = await proc.wait()
                stderr = (await stderr_task).decode(errors="replace")
                if any(marker in stderr for marker in _PORTAL_DECLINED):
                    raise PickCancelled()
                raise CaptureError(
                    f"screen capture failed (exit {code}): {_last_message(stderr)}"
                )
            buf += chunk
            starts = pacer.find_start_codes(buf)
            # Only NALs followed by another start code are complete.
            complete = [
                buf[pos + length : starts[i + 1][0]]
                for i, (pos, length) in enumerate(starts[:-1])
            ]
            sps = find_sps(complete)
            if sps is not None:
                return sps_dimensions(sps)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        stderr_task.cancel()


def _die_with_parent(parent: Optional[int] = None) -> None:
    die_with_parent(signal.SIGKILL, parent)


def child_setup() -> Callable[[], None]:
    """A ``preexec_fn`` that ties a child to this process.

    It names this process as the parent from here, before the fork: a parent
    killed before the child's prctl is then still caught.
    """
    return functools.partial(_die_with_parent, os.getpid())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omashare.capture")
    parser.add_argument("--target", required=True, help=f"monitor or {PORTAL}")
    parser.add_argument("--portal-token", type=Path)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-size", default="1920x1080")
    parser.add_argument("--no-cursor", action="store_true")
    args = parser.parse_args(argv)
    max_w, max_h = (int(v) for v in args.max_size.split("x"))

    _die_with_parent()
    gsr_argv, ffmpeg_argv = pipeline_argv(
        args.target, args.fps, max_w, max_h, not args.no_cursor, args.portal_token
    )
    # gsr's messages go to our stderr: the session discards them, the probe
    # (pick_source) reads them to tell a cancelled dialog from a failure.
    try:
        gsr = subprocess.Popen(
            gsr_argv, stdout=subprocess.PIPE, preexec_fn=child_setup()
        )
    except FileNotFoundError:
        print("omashare: gpu-screen-recorder is not installed", file=sys.stderr)
        return 127
    try:
        ffmpeg = subprocess.Popen(
            ffmpeg_argv, stdin=gsr.stdout, preexec_fn=child_setup()
        )
    except FileNotFoundError:
        gsr.kill()
        print("omashare: ffmpeg is not installed", file=sys.stderr)
        return 127
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
    gsr_code = gsr.wait()
    # A gsr failure is the cause; ffmpeg only saw its output end.
    return gsr_code if gsr_code not in (0, -signal.SIGKILL) else code


if __name__ == "__main__":
    sys.exit(main())
