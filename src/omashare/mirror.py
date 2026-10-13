"""Drive one pyatv mirror session to an AirPlay receiver.

Follows the order the mirror package requires on a fresh connection: media
pair-verify, FPLY handshake, then :class:`MirrorSession` with the live
screen capture as ``video_command`` and the screen audio as ``audio_command``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable

from pyatv.protocols.airplay.mirror import (
    MirrorContext,
    media_pair_verify,
    run_fply_handshake,
)
from pyatv.protocols.airplay.mirror.framing import MirrorEncryptor
from pyatv.support.http import http_connect

from omashare import audio, capture
from omashare.sink import AudioRoute
from omashare.tvos27 import StreamRtspSession, Tvos27MirrorSession

_LOGGER = logging.getLogger(__name__)

AIRPLAY_PORT = 7000

#: What to share: the dialog's pick, or the focused monitor without asking.
SOURCES = ("picker", "monitor")

Emit = Callable[..., None]


def runtime_dir() -> Path:
    path = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/omashare-{os.getuid()}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    path = Path(base) / "omashare"
    path.mkdir(parents=True, exist_ok=True)
    return path


def placeholder_h264() -> Path:
    """A tiny valid H.264 file.

    MirrorSession validates ``h264_path`` (SPS, PPS and an IDR) even when a
    live ``video_command`` is streamed instead, so one has to exist.
    """
    path = cache_dir() / "placeholder.h264"
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=black:size=320x180:rate=30",
                "-t", "1", "-c:v", "libx264", "-profile:v", "baseline",
                "-g", "30", "-bf", "0", "-pix_fmt", "yuv420p",
                "-f", "h264", str(tmp),
            ],
            check=True,
        )  # fmt: skip
        tmp.rename(path)
    return path


async def mirror(
    host: str,
    port: int = AIRPLAY_PORT,
    *,
    monitor: str | None = None,
    fps: int = 30,
    max_size: tuple[int, int] = (1920, 1080),
    cursor: bool = True,
    audio_mode: str = "tv",
    source: str = "picker",
    emit: Emit = lambda *a, **k: None,
) -> None:
    """Mirror until cancelled.

    *source* is one of :data:`SOURCES`: ``picker`` shows the screen-share
    dialog first (and returns quietly if it is cancelled), ``monitor``
    shares *monitor*, by default the focused one. *audio_mode* is one of
    :data:`omashare.sink.MODES`.
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
    portal_token: Path | None = None
    if source == "picker":
        # Picked before connecting, so the receiver sees nothing meanwhile.
        portal_token = runtime_dir() / "omashare-portal-token"
        emit("choosing")
        try:
            width, height = await capture.pick_source(
                portal_token, fps, *max_size, cursor
            )
        except capture.PickCancelled:
            emit("ended", reason="cancelled")
            return
        target, label = capture.PORTAL, "your selection"
    else:
        mon = capture.focused_monitor()
        if monitor and monitor != mon.name:
            mon = capture.Monitor(monitor, mon.width, mon.height)
        width, height = capture.fit_size(mon.width, mon.height, *max_size)
        target, label = mon.name, mon.name
    h264_path = placeholder_h264()

    emit("connecting", host=host, port=port)
    connection = await http_connect(host, port)
    session = None
    route = contextlib.ExitStack()
    try:
        rtsp = StreamRtspSession(connection)

        emit("pairing")
        pair_secret = await media_pair_verify(rtsp)
        handshake = await run_fply_handshake(connection)

        ctx = MirrorContext(width=width, height=height, fps=fps, name="Omarchy")
        # Only a fallback cipher; the video key is derived from raw16 and
        # the pair-verify secret inside the session.
        ctx.stream_encryptor = MirrorEncryptor.from_key_iv(
            handshake.stream_aes_key, handshake.stream_aes_iv
        )
        ctx.sap_context = handshake.sap_context
        ctx.stream_raw16 = handshake.chosen_raw16
        ctx.ekey = handshake.ekey
        ctx.audio_ekey = handshake.audio_ekey

        # Routed only now, so a failed handshake never touches local audio.
        source = route.enter_context(AudioRoute(audio_mode))
        session = Tvos27MirrorSession(
            rtsp,
            None,
            ctx,
            h264_path,
            video_command=capture.video_command(
                target, fps, max_size[0], max_size[1], cursor, portal_token
            ),
            audio_command=audio.audio_command(source) if source else None,
            pair_secret=pair_secret,
        )
        emit(
            "streaming",
            monitor=label,
            width=width,
            height=height,
            fps=fps,
            audio=audio_mode,
        )
        await session.run()
        emit("ended", reason="receiver closed the session")
    finally:
        try:
            if session is not None:
                # Shielded: stop() sends TEARDOWN and must survive our cancel.
                await asyncio.shield(session.stop())
        finally:
            # Local audio is restored whatever happened to the session.
            route.close()
            connection.close()
