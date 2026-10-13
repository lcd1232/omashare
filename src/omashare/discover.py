"""Find AirPlay receivers on the local network."""

from __future__ import annotations

import asyncio

import pyatv
from pyatv.const import Protocol


async def scan(timeout: float = 3.0) -> list[dict]:
    """Return the AirPlay receivers that answer within *timeout* seconds."""
    loop = asyncio.get_running_loop()
    confs = await pyatv.scan(loop, timeout=timeout, protocol=Protocol.AirPlay)
    devices = []
    for conf in confs:
        service = conf.get_service(Protocol.AirPlay)
        if service is None:
            continue
        devices.append(
            {
                "name": conf.name,
                "address": str(conf.address),
                "port": service.port,
                "identifier": conf.identifier,
                "model": conf.device_info.model_str,
            }
        )
    devices.sort(key=lambda d: d["name"].lower())
    return devices
