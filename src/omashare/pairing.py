"""One-time PIN pairing of omashare with a receiver."""

from __future__ import annotations

from typing import Awaitable, Callable

from pyatv.protocols.airplay.mirror import MediaIdentity, register_media_identity
from pyatv.support.http import http_connect
from pyatv.support.rtsp import RtspSession

from omashare.devices import DeviceStore


async def pair(
    host: str,
    port: int,
    device_id: str,
    name: str,
    pin: Callable[[], Awaitable[str]],
    store: DeviceStore,
) -> MediaIdentity:
    """Register a new identity with the receiver and store it for *device_id*.

    *pin* is awaited once the receiver shows its PIN and returns it as a
    string. Raises pyatv's AuthenticationError if the receiver refuses (for
    instance a wrong PIN); nothing is stored then.
    """
    identity = MediaIdentity.generate()
    connection = await http_connect(host, port)
    try:
        await register_media_identity(RtspSession(connection), identity, pin)
    finally:
        connection.close()
    store.save(device_id, name, identity)
    return identity
