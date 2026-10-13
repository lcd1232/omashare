"""The media identities omashare has paired with each receiver.

One identity per receiver, created by ``omashare pair`` and the PIN the
receiver shows. The seeds are secrets: anyone holding one can mirror to that
receiver, so the file is private to the user (mode 600, in a 700 directory).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from pyatv.protocols.airplay.mirror import MediaIdentity


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "omashare"


def _key(device_id: str) -> str:
    return device_id.strip().upper()


class DeviceStore:
    """``devices.json``: ``{device id: {"name": ..., "identity": seed hex}}``."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or config_dir() / "devices.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def identity(self, device_id: str) -> Optional[MediaIdentity]:
        """The identity paired with *device_id*, or None if it isn't paired."""
        entry = self._load().get(_key(device_id))
        try:
            return MediaIdentity.from_hex(entry["identity"]) if entry else None
        except (KeyError, TypeError, ValueError):
            return None

    def paired(self) -> set[str]:
        """Ids of every receiver with a stored identity."""
        return {k for k in self._load() if self.identity(k) is not None}

    def save(self, device_id: str, name: str, identity: MediaIdentity) -> None:
        data = self._load()
        data[_key(device_id)] = {"name": name, "identity": identity.to_hex()}
        self._write(data)

    def forget(self, device_id: str) -> bool:
        """Drop the identity of *device_id*; True if there was one."""
        data = self._load()
        if data.pop(_key(device_id), None) is None:
            return False
        self._write(data)
        return True
