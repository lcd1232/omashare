"""Find AirPlay receivers on the local network.

Through the system's mDNS daemon (``avahi-browse``) when it is there. pyatv's
own scanner queries from a socket of its own, and receivers answer that
socket directly; a host firewall that only lets in known receivers (as
Omarchy's ufw setup does) drops those answers, so other receivers never show
up. avahi's traffic is allowed, so it sees every receiver. Without avahi,
pyatv's scanner is used.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from ipaddress import ip_address
from typing import Optional

import pyatv
from pyatv.const import Protocol
from pyatv.interface import DeviceInfo
from pyatv.support.device_info import lookup_model

_LOGGER = logging.getLogger(__name__)

#: AirPlay ``features`` bit for screen mirroring; audio-only receivers
#: (HomePods, AirPlay speakers) lack it.
FEATURE_SCREEN = 1 << 7

_ESCAPE = re.compile(rb"\\(\d{3})")


def _unescape(text: str) -> str:
    """Undo avahi's ``\\DDD`` (decimal byte) escapes, e.g. ``\\032`` for a space."""
    raw = _ESCAPE.sub(lambda m: bytes([int(m.group(1))]), text.encode("utf-8"))
    return raw.decode("utf-8", errors="replace")


def _txt(field: str) -> dict:
    """Parse avahi's TXT field: ``"k=v" "k=v" ...``."""
    record = {}
    for item in re.findall(r'"((?:[^"\\]|\\.)*)"', field):
        key, _, value = item.partition("=")
        record[key.lower()] = value
    return record


def _features(value: str) -> int:
    """``features=0xLOW,0xHIGH`` as one integer (only the low word matters here)."""
    try:
        return int(value.split(",")[0], 16)
    except ValueError:
        return 0


def _model_str(raw: str) -> str:
    if not raw:
        return ""
    return DeviceInfo({DeviceInfo.MODEL: lookup_model(raw), DeviceInfo.RAW_MODEL: raw}).model_str


def parse_avahi(output: str) -> list[dict]:
    """Receivers that can show a mirrored screen, from ``avahi-browse -rtpk``.

    A receiver appears once per interface and IP version; they are merged by
    device id, preferring an IPv4 address.
    """
    found: dict[str, dict] = {}
    for line in output.splitlines():
        parts = line.split(";")
        if len(parts) < 10 or parts[0] != "=" or parts[4] != "_airplay._tcp":
            continue
        txt = _txt(";".join(parts[9:]))
        if not _features(txt.get("features", "0")) & FEATURE_SCREEN:
            continue
        device_id = (txt.get("deviceid") or parts[6]).upper()
        try:
            address = ip_address(parts[7].split("%")[0])
        except ValueError:
            continue
        device = {
            "name": _unescape(parts[3]),
            "address": str(address),
            "port": int(parts[8]) if parts[8].isdigit() else 7000,
            "identifier": device_id,
            "model": _model_str(txt.get("model", "")),
        }
        known = found.get(device_id)
        if known is None or (address.version == 4 and ":" in known["address"]):
            found[device_id] = device
    return sorted(found.values(), key=lambda d: d["name"].lower())


async def _avahi(timeout: float) -> Optional[list[dict]]:
    """Receivers from avahi, or None when avahi can't be asked."""
    if shutil.which("avahi-browse") is None:
        return None
    proc = await asyncio.create_subprocess_exec(
        # -r resolve, -t stop after the cache dump, -p parsable, -k raw types.
        "avahi-browse", "-rtpk", "_airplay._tcp",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        out, _ = await proc.communicate()
    if proc.returncode not in (0, None, -9) and not out:
        _LOGGER.info("avahi-browse failed (%s); using pyatv's scanner", proc.returncode)
        return None
    return parse_avahi(out.decode("utf-8", errors="replace"))


async def _pyatv(timeout: float, hosts: Optional[list[str]]) -> list[dict]:
    loop = asyncio.get_running_loop()
    confs = await pyatv.scan(
        loop, timeout=timeout, protocol=Protocol.AirPlay, hosts=hosts
    )
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


async def scan(timeout: float = 3.0, hosts: Optional[list[str]] = None) -> list[dict]:
    """Return the AirPlay receivers that can show a mirrored screen.

    With *hosts*, only receivers at those addresses.
    """
    devices = await _avahi(timeout)
    if devices is not None:
        if hosts:
            devices = [d for d in devices if d["address"] in hosts]
        if devices or not hosts:
            return devices
    return await _pyatv(timeout, hosts)
