"""The control connection to a receiver."""

from __future__ import annotations

import asyncio

from pyatv.support.http import HttpConnection, http_connect

#: Seconds to wait for the receiver to accept the connection. Without a
#: limit an unreachable receiver takes the OS's SYN timeout, about 2 minutes.
CONNECT_TIMEOUT = 10.0


async def connect(host: str, port: int) -> HttpConnection:
    """Open the RTSP/HTTP connection to *host*:*port*, or raise ConnectionError."""
    try:
        return await asyncio.wait_for(http_connect(host, port), CONNECT_TIMEOUT)
    except TimeoutError as exc:
        # Not a TimeoutError: the CLI reads that as a receiver gone quiet
        # mid-session, which points at the firewall.
        raise ConnectionError(f"{host} did not answer on port {port}") from exc
