"""Where screen audio is captured from, and whether it still plays locally.

``tv``: audio goes only to the receiver, as AirPlay does on a Mac. A null
sink is loaded and made the default while sharing, and its monitor is
captured. ``both``: the default sink's monitor is captured, so sound plays
locally too. ``off``: no screen audio.

If EasyEffects is running, its input (the apps' mix before any effects) is
captured instead. Its presets are tuned for the local speakers -- on a
laptop typically +15 dB of bass and scooped mids -- and sound wrong on a TV.

The null sink, the previous default and the owning process are recorded in a
state file, so a helper that was killed without restoring them is cleaned up
on the next run -- but never while that process is still alive.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

_LOGGER = logging.getLogger(__name__)

SINK_NAME = "omashare"
#: EasyEffects' virtual sink; apps play into it and it plays the processed
#: result into the default output.
EFFECTS_SINK = "easyeffects_sink"
MODES = ("tv", "both", "off")

Runner = Callable[[list[str]], str]


def _pactl(args: list[str]) -> str:
    return subprocess.run(
        ["pactl", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _state_file() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/omashare-{os.getuid()}"
    return Path(base) / "omashare-sink.json"


class AudioRoute:
    """Context manager yielding the PulseAudio source to capture, or None."""

    def __init__(
        self,
        mode: str,
        *,
        bypass_effects: bool = True,
        pactl: Runner = _pactl,
        state_file: Optional[Path] = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"audio mode must be one of {MODES}, got {mode!r}")
        self.mode = mode
        self.bypass_effects = bypass_effects
        self._pactl = pactl
        self._state_file = state_file or _state_file()
        self._module: Optional[str] = None
        self._previous: Optional[str] = None

    def __enter__(self) -> Optional[str]:
        restore_stale(self._pactl, self._state_file)
        if self.mode == "off":
            return None
        default = self._pactl(["get-default-sink"])
        effects = self._effects_monitor()
        if self.mode == "both":
            return effects or f"{default}.monitor"

        self._previous = default
        self._module = self._pactl(
            [
                "load-module",
                "module-null-sink",
                f"sink_name={SINK_NAME}",
                "sink_properties=device.description=AirPlay",
            ]
        )
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(
            json.dumps(
                {"module": self._module, "previous": default, "pid": os.getpid()}
            )
        )
        self._pactl(["set-default-sink", SINK_NAME])
        # Streams pinned to a sink don't follow the default; move them over.
        for line in self._pactl(["list", "short", "sink-inputs"]).splitlines():
            stream_id = line.split("\t", 1)[0]
            if stream_id.isdigit():
                self._try(["move-sink-input", stream_id, SINK_NAME])
        _LOGGER.info("audio: routed output to %s (was %s)", SINK_NAME, default)
        return effects or f"{SINK_NAME}.monitor"

    def _effects_monitor(self) -> Optional[str]:
        """The monitor of EasyEffects' input sink, if it should be captured."""
        if not self.bypass_effects:
            return None
        try:
            sinks = self._pactl(["list", "short", "sinks"])
        except (subprocess.CalledProcessError, OSError):
            return None
        names = {line.split("\t")[1] for line in sinks.splitlines() if "\t" in line}
        if EFFECTS_SINK in names:
            _LOGGER.info("audio: capturing %s, before EasyEffects", EFFECTS_SINK)
            return f"{EFFECTS_SINK}.monitor"
        return None

    def __exit__(self, *exc) -> None:
        if self._module is None:
            return
        # Unloading the null sink moves its streams back to the default.
        if self._previous:
            self._try(["set-default-sink", self._previous])
        self._try(["unload-module", self._module])
        self._state_file.unlink(missing_ok=True)
        _LOGGER.info("audio: restored output to %s", self._previous)
        self._module = None

    def _try(self, args: list[str]) -> None:
        try:
            self._pactl(args)
        except (subprocess.CalledProcessError, OSError) as exc:
            _LOGGER.warning("pactl %s failed: %s", " ".join(args), exc)


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def restore_stale(pactl: Runner = _pactl, state_file: Optional[Path] = None) -> None:
    """Undo a null sink left behind by a helper that didn't exit cleanly.

    A sink whose owner is still running is in use, not stale, and is kept.
    """
    path = state_file or _state_file()
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if _alive(state.get("pid")):
        return
    _LOGGER.info("audio: cleaning up a stale %s sink", SINK_NAME)
    for args in (
        ["set-default-sink", state.get("previous", "")],
        ["unload-module", str(state.get("module", ""))],
    ):
        if args[-1]:
            try:
                pactl(args)
            except (subprocess.CalledProcessError, OSError):
                pass
    path.unlink(missing_ok=True)
