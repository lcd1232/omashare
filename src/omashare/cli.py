"""omashare command line, also the backend of the Omarchy bar widget.

Every command prints JSON to stdout. ``mirror`` prints one JSON object per
line as the session progresses (``{"event": "streaming", ...}``) and runs
until SIGTERM/SIGINT or the receiver ends the session.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

from omashare.sink import MODES, restore_stale


def _runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/omashare-{os.getuid()}")


PID_FILE = _runtime_dir() / "omashare.pid"


def _print(obj) -> None:
    print(json.dumps(obj), flush=True)


def _emit(event: str, **fields) -> None:
    _print({"event": event, **fields})


def _read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _restore_audio_if_idle() -> None:
    """Undo a null sink left by a killed mirror, so local sound comes back
    even if sharing is never started again. Never while a mirror runs."""
    if _read_pid() is None:
        restore_stale()


def cmd_scan(args) -> int:
    from omashare.discover import scan

    _restore_audio_if_idle()
    _print(asyncio.run(scan(args.timeout)))
    return 0


def cmd_status(_args) -> int:
    _restore_audio_if_idle()
    pid = _read_pid()
    _print({"running": pid is not None, "pid": pid})
    return 0


def cmd_stop(_args) -> int:
    pid = _read_pid()
    if pid is not None:
        os.kill(pid, signal.SIGTERM)
    _print({"stopped": pid is not None})
    return 0


def cmd_mirror(args) -> int:
    from omashare.mirror import mirror

    if (pid := _read_pid()) is not None:
        _emit("error", message=f"already mirroring (pid {pid}); run 'omashare stop'")
        return 1
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    max_w, max_h = (int(v) for v in args.max_size.split("x"))

    async def run() -> int:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await mirror(
                args.host,
                args.port,
                monitor=args.monitor,
                fps=args.fps,
                max_size=(max_w, max_h),
                cursor=not args.no_cursor,
                audio_mode=args.audio,
                source=args.source,
                emit=_emit,
            )
        except asyncio.CancelledError:
            _emit("ended", reason="stopped")
        except Exception as exc:  # noqa: BLE001 - reported to the widget
            logging.getLogger(__name__).exception("mirror failed")
            _emit("error", message=f"{type(exc).__name__}: {exc}")
            return 1
        return 0

    try:
        return asyncio.run(run())
    finally:
        PID_FILE.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omashare")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug log")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="list AirPlay receivers as JSON")
    p.add_argument("--timeout", type=float, default=3.0)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("mirror", help="mirror the screen to a receiver")
    p.add_argument("host")
    p.add_argument("--port", type=int, default=7000)
    p.add_argument(
        "--source",
        choices=("picker", "monitor"),
        default="picker",
        help="picker: choose in the screen-share dialog; monitor: share --monitor",
    )
    p.add_argument("--monitor", help="Hyprland output for --source monitor (default: focused)")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--max-size", default="1920x1080", help="WxH limit")
    p.add_argument("--no-cursor", action="store_true")
    p.add_argument(
        "--audio",
        choices=MODES,
        default="tv",
        help="tv: only on the receiver; both: also locally; off: no audio",
    )
    p.set_defaults(func=cmd_mirror)

    sub.add_parser("status", help="is a mirror running").set_defaults(func=cmd_status)
    sub.add_parser("stop", help="stop the running mirror").set_defaults(func=cmd_stop)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.command == "mirror")
    return args.func(args)


def log_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "omashare" / "omashare.log"


def _setup_logging(verbose: bool, to_file: bool) -> None:
    """Log to stderr (stdout is reserved for JSON) and, for ``mirror``, to a
    debug log file, since the bar widget does not keep the helper's stderr."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.DEBUG if verbose else logging.INFO)
    stderr.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(stderr)
    if to_file:
        path = log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2_000_000, backupCount=2
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(fmt)
        root.addHandler(handler)


if __name__ == "__main__":
    sys.exit(main())
