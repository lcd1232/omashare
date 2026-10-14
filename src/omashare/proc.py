"""Where omashare keeps its runtime files, and which process owns them."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional


def runtime_dir() -> Path:
    """A directory only this user can write, for PID and state files.

    ``$XDG_RUNTIME_DIR`` when set; otherwise ``/tmp/omashare-<uid>``, created
    private and refused if someone else owns it or can write to it (another
    local user could otherwise plant state files there).
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base)
    path = Path(f"/tmp/omashare-{os.getuid()}")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise RuntimeError(f"{path} is not a private directory of this user")
    return path


def start_time(pid: int) -> Optional[str]:
    """The process's start time (clock ticks since boot), or None if gone.

    Together with the pid it names one process; a pid alone can be reused.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    # Field 22 overall; fields[0] here is field 3 (state).
    return fields[19] if len(fields) > 19 else None


def this_process() -> dict:
    """``{"pid", "start"}`` of the calling process, for an owner record."""
    pid = os.getpid()
    return {"pid": pid, "start": start_time(pid)}


def is_running(pid: object, start: object = None) -> bool:
    """True if *pid* runs and, when *start* is given, is that same process."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    current = start_time(pid)
    if current is None:
        return False
    return start is None or current == start
