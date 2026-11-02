import asyncio

import pytest

from omashare import connect


def test_an_unreachable_receiver_fails_fast(monkeypatch):
    async def never(host, port):
        await asyncio.Event().wait()

    monkeypatch.setattr(connect, "http_connect", never)
    monkeypatch.setattr(connect, "CONNECT_TIMEOUT", 0.01)
    with pytest.raises(ConnectionError, match="192.0.2.1 did not answer on port 7000"):
        asyncio.run(connect.connect("192.0.2.1", 7000))
