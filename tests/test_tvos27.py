import asyncio
import plistlib

import pytest

from pyatv.protocols.airplay.mirror import MirrorContext
from pyatv.protocols.airplay.mirror.framing import MirrorEncryptor
from pyatv.support.http import HttpResponse

from omashare.tvos27 import (
    AUDIO_STREAM_EXTRA,
    StreamRtspSession,
    Tvos27MirrorSession,
)

SESSION_EVENT_PORT = 49286


class FakeConnection:
    """Records requests; answers SETUPs like the Apple TV did."""

    local_ip = "127.0.0.1"
    remote_ip = "127.0.0.1"

    def __init__(self):
        self.requests = []

    async def send_and_receive(self, method, uri, headers=None, body=None, **kwargs):
        decoded = plistlib.loads(body) if isinstance(body, bytes) and body else None
        self.requests.append((method, uri, dict(headers or {}), decoded))
        reply = {}
        if method == "SETUP":
            streams = (decoded or {}).get("streams")
            if not streams:
                reply = {"skipRecord": True, "eventPort": SESSION_EVENT_PORT}
            elif streams[0]["type"] == 96:
                # tvOS 27 no longer names the event port here.
                reply = {"streams": [{"type": 96, "dataPort": 50001, "controlPort": 50002}]}
            else:
                reply = {"streams": [{"type": 110, "dataPort": 50003}]}
        return HttpResponse(
            "RTSP",
            "1.0",
            200,
            "OK",
            {"CSeq": str(headers["CSeq"])},
            plistlib.dumps(reply, fmt=plistlib.FMT_BINARY),
        )


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def conn():
    return FakeConnection()


def test_stream_setup_goes_to_its_own_uri_with_the_shared_eiv(conn):
    rtsp = StreamRtspSession(conn)
    body = {
        "streams": [{"type": 96, "streamConnectionID": 2003288152}],
        "eiv": b"\x01" * 16,
        "ekey": b"k" * 72,
    }
    run(rtsp.setup(headers={"X": "1"}, body=body))
    method, uri, headers, sent = conn.requests[0]
    assert (method, uri) == ("SETUP", "rtsp://127.0.0.1/2003288152")
    assert sent["eiv"] == rtsp.stream_eiv
    assert headers["rmodel"] == "PC1,1"
    assert headers["X"] == "1"
    for key, value in AUDIO_STREAM_EXTRA.items():
        assert sent["streams"][0][key] == value


def test_video_setup_gets_no_audio_fields(conn):
    rtsp = StreamRtspSession(conn)
    body = {"streams": [{"type": 110, "streamConnectionID": 7}], "eiv": b"\x02" * 16}
    run(rtsp.setup(body=body))
    _, uri, _, sent = conn.requests[0]
    assert uri == "rtsp://127.0.0.1/7"
    assert sent["eiv"] == rtsp.stream_eiv
    assert "isMedia" not in sent["streams"][0]


def test_setup_without_streams_is_left_alone(conn):
    rtsp = StreamRtspSession(conn)
    session_id = rtsp.session_id
    run(rtsp.setup(body={"et": 32}))
    method, uri, headers, sent = conn.requests[0]
    assert uri == f"rtsp://127.0.0.1/{session_id}"
    assert "rmodel" not in headers
    assert sent == {"et": 32}


def test_session_needs_a_stream_rtsp_session(conn):
    from pyatv.support.rtsp import RtspSession

    with pytest.raises(TypeError):
        Tvos27MirrorSession(RtspSession(conn), None, MirrorContext(), "x.h264")


def test_setup_order_event_port_and_audio_iv(conn, tmp_path):
    rtsp = StreamRtspSession(conn)
    ctx = MirrorContext(stream_encryptor=MirrorEncryptor.from_key_iv(b"\0" * 16, b"\0" * 16))
    ctx.ekey = ctx.audio_ekey = b"e" * 72
    session = Tvos27MirrorSession(rtsp, None, ctx, tmp_path / "x.h264")

    async def setups():
        try:
            await session._setup_session()
            await session._setup_audio_stream()
        finally:
            session._close_endpoints()

    run(setups())

    (_, uri0, _, first), (_, uri1, _, audio) = conn.requests
    # The session SETUP comes first and carries no streams.
    assert "streams" not in first
    assert first["isScreenMirroringSession"] is True
    assert first["timingProtocol"] == "NTP"
    assert first["eiv"] == rtsp.stream_eiv
    # The audio SETUP follows on its own URI.
    assert uri1.endswith(f"/{audio['streams'][0]['streamConnectionID']}")
    assert audio["streams"][0]["isMedia"] is True
    # The event port is the session SETUP's, and audio uses the shared eiv.
    assert ctx.event_port == SESSION_EVENT_PORT
    assert ctx.audio_eiv == rtsp.stream_eiv == audio["eiv"]
