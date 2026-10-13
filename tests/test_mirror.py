import asyncio

import pytest

from pyatv.protocols.airplay.mirror import MediaIdentity

from omashare import capture, mirror


def test_cancelled_pick_ends_quietly_without_connecting(monkeypatch, tmp_path):
    async def cancelled(*args):
        raise capture.PickCancelled()

    async def must_not_connect(*args):
        raise AssertionError("connected to the receiver after a cancelled pick")

    monkeypatch.setattr(capture, "pick_source", cancelled)
    monkeypatch.setattr(mirror, "http_connect", must_not_connect)
    monkeypatch.setattr(mirror, "runtime_dir", lambda: tmp_path)
    events = []
    asyncio.run(
        mirror.mirror(
            "192.0.2.1",
            identity=MediaIdentity.generate(),
            emit=lambda e, **k: events.append((e, k)),
        )
    )
    assert events == [("choosing", {}), ("ended", {"reason": "cancelled"})]


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError):
        asyncio.run(
            mirror.mirror("192.0.2.1", identity=MediaIdentity.generate(), source="webcam")
        )
