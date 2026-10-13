"""What tvOS 27 needs before it plays mirrored audio.

The pyatv mirror sender copies AirParrot 3.1.7, whose type-96 screen audio
tvOS 27 silently ignores: the SETUP succeeds, packets arrive and nothing
plays. AirParrot 3.1.9 plays again; a capture of it against an Apple TV 4K
on tvOS 27 differs in four structural ways, and those four are enough:

1. A session SETUP with no streams comes first. Its reply carries the
   ``eventPort`` (the stream SETUPs no longer do).
2. Each stream SETUP goes to its own URI, ``rtsp://<ip>/<streamConnectionID>``;
   RECORD follows on the audio stream's URI.
3. The audio and video SETUPs share one ``eiv``, which is also the screen
   audio's AES-CBC IV.
4. The audio stream adds ``isMedia``, ``sr`` and ``audioMode``, and stream
   SETUPs carry an ``rmodel`` header.

AirParrot also sends every audio packet twice, zeroes the NTP time in sync
packets, skips the event channel and uses its own identity. Audio plays
without any of that, so none of it is copied.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, Optional, Union

from pyatv import exceptions
from pyatv.protocols.airplay.mirror import session as mirror_session
from pyatv.protocols.airplay.mirror.session import MirrorSession
from pyatv.support.http import HttpResponse, decode_bplist_from_body
from pyatv.support.rtsp import RtspSession

_LOGGER = logging.getLogger(__name__)

AUDIO_STREAM_TYPE = 96
VIDEO_STREAM_TYPE = 110

#: Fields tvOS 27 wants on the type-96 screen-audio stream.
AUDIO_STREAM_EXTRA = {"isMedia": True, "sr": 44100, "audioMode": "default"}

#: Header AirParrot 3.1.9 adds to its stream SETUPs.
STREAM_SETUP_HEADERS = {"rmodel": "PC1,1"}


class StreamRtspSession(RtspSession):
    """RtspSession that sends stream SETUPs the way tvOS 27 accepts them."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: The one eiv of every stream SETUP, and the screen-audio CBC IV.
        self.stream_eiv: bytes = secrets.token_bytes(16)

    async def setup(
        self,
        headers: Optional[Dict[str, Any]] = None,
        body: Optional[Union[str, bytes, dict]] = None,
        timeout: float = 4.0,
    ) -> HttpResponse:
        streams = body.get("streams") if isinstance(body, dict) else None
        if streams:
            stream = streams[0]
            # The URI names the stream; later requests (RECORD) reuse it.
            self.session_id = stream["streamConnectionID"]
            if "eiv" in body:
                body["eiv"] = self.stream_eiv
            if stream.get("type") == AUDIO_STREAM_TYPE:
                stream.update(AUDIO_STREAM_EXTRA)
            headers = {**(headers or {}), **STREAM_SETUP_HEADERS}
        return await super().setup(headers=headers, body=body, timeout=timeout)


class Tvos27MirrorSession(MirrorSession):
    """MirrorSession that opens the session the way tvOS 27 expects.

    Needs a :class:`StreamRtspSession` as its RTSP session.
    """

    def __init__(self, rtsp: StreamRtspSession, *args, **kwargs) -> None:
        if not isinstance(rtsp, StreamRtspSession):
            raise TypeError("Tvos27MirrorSession needs a StreamRtspSession")
        super().__init__(rtsp, *args, **kwargs)
        self._stream_rtsp = rtsp
        self._session_event_port = 0

    async def _setup_session(self) -> None:
        await super()._setup_session()
        rtsp = self._stream_rtsp
        body = {
            "et": mirror_session.STREAM_ET,
            "timingPort": self._timing_server.port if self._timing_server else 0,
            "timingProtocol": "NTP",
            "isScreenMirroringSession": True,
            "sessionUUID": self._session_uuid,
            "osBuildVersion": self._ctx.os_build_version,
            "sourceVersion": self._ctx.source_version,
            "deviceID": self._device_id,
            "macAddress": self._mac_address,
            "name": self._ctx.name,
            "model": self._ctx.model,
        }
        if self._ctx.ekey:
            body["ekey"] = self._ctx.ekey
            body["eiv"] = rtsp.stream_eiv
        resp = await rtsp.setup(
            headers=dict(mirror_session._SUPPRESS_RAOP_HEADERS),  # pylint: disable=protected-access
            body=body,
            timeout=mirror_session.STREAM_SETUP_TIMEOUT,
        )
        if resp.code != 200:
            raise exceptions.ProtocolError(f"session SETUP failed: HTTP {resp.code}")
        decoded = decode_bplist_from_body(resp)
        self._session_event_port = decoded.get("eventPort", 0)
        _LOGGER.debug("session SETUP: %r", decoded)

    async def _setup_audio_stream(self) -> None:
        await super()._setup_audio_stream()
        # The audio SETUP reply no longer names the event port; keep the
        # session SETUP's. Encrypt screen audio with the shared eiv.
        if not self._ctx.event_port:
            self._ctx.event_port = self._session_event_port
        self._ctx.audio_eiv = self._stream_rtsp.stream_eiv
