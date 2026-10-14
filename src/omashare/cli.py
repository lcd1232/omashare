"""omashare command line, also the backend of the Omarchy bar widget.

Every command prints JSON to stdout. ``mirror`` prints one JSON object per
line as the session progresses (``{"event": "streaming", ...}``) and runs
until SIGTERM/SIGINT or the receiver ends the session. ``pair`` prints
``{"event": "pin"}`` once the receiver shows its PIN, reads the PIN from a
line on stdin, and ends with ``{"event": "paired"}``. A receiver that is not
paired yet makes ``mirror`` fail with ``{"event": "error", "code":
"not-paired"}``. ``firewall`` says whether the host firewall lets receivers
reach the timing port.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

from pyatv import exceptions

from omashare import discover, firewall, mirror, pairing
from omashare.devices import DeviceStore
from omashare.firewall import DEFAULT_TIMING_PORT
from omashare.proc import die_with_parent, is_running, runtime_dir, this_process
from omashare.sink import MODES, restore_stale

PID_FILE = runtime_dir() / "omashare.pid"


def _print(obj) -> None:
    print(json.dumps(obj), flush=True)


def _emit(event: str, **fields) -> None:
    _print({"event": event, **fields})


def _read_owner() -> dict | None:
    """The running mirror's record (pid, start time, receiver), or None."""
    try:
        record = json.loads(PID_FILE.read_text())
    except (OSError, ValueError):
        return None
    # Without its start time a pid could name an unrelated, newer process.
    if not isinstance(record, dict) or not record.get("start"):
        return None
    if not is_running(record.get("pid"), record["start"]):
        return None
    return record


def _claim_pid_file(record: dict) -> object | None:
    """Become the one running mirror: lock the PID file and write *record*.

    Returns the open file, which holds the lock until this process exits (the
    kernel drops it even if the process is killed), or None if another mirror
    holds it.
    """
    while True:
        fh = open(PID_FILE, "a+")  # noqa: SIM115 - held for the process's life
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return None
        try:
            same = os.fstat(fh.fileno()).st_ino == PID_FILE.stat().st_ino
        except FileNotFoundError:
            same = False
        if same:
            break
        fh.close()  # the previous owner removed it meanwhile: start over
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps(record))
    fh.flush()
    return fh


def _read_pid() -> int | None:
    owner = _read_owner()
    return owner["pid"] if owner else None


def _restore_audio_if_idle() -> None:
    """Undo a null sink left by a killed mirror, so local sound comes back
    even if sharing is never started again. Never while a mirror runs."""
    if _read_pid() is None:
        restore_stale()


def cmd_scan(args) -> int:
    _restore_audio_if_idle()
    devices = asyncio.run(discover.scan(args.timeout))
    paired = DeviceStore().paired()
    for device in devices:
        device["paired"] = str(device["identifier"]).upper() in paired
    # Paired receivers first; scan() already sorted by name, and sort is stable.
    devices.sort(key=lambda d: not d["paired"])
    _print(devices)
    return 0


def _resolve(args) -> tuple[str, str]:
    """The receiver's (identifier, name), from the arguments or by asking it."""
    if args.id:
        return args.id, args.name or args.host
    found = asyncio.run(discover.scan(3.0, hosts=[args.host]))
    if not found:
        raise LookupError(f"no AirPlay receiver answered at {args.host}")
    return found[0]["identifier"], args.name or found[0]["name"]


def cmd_pair(args) -> int:
    try:
        device_id, name = _resolve(args)
    except LookupError as exc:
        _emit("error", message=str(exc))
        return 1

    asked = False

    async def pin() -> str:
        nonlocal asked
        asked = True
        _emit("pin", id=device_id, name=name)
        line = await asyncio.get_running_loop().run_in_executor(
            None, sys.stdin.readline
        )
        if not line.strip():
            raise exceptions.AuthenticationError("pairing cancelled")
        return line.strip()

    async def run() -> int:
        try:
            await pairing.pair(args.host, args.port, device_id, name, pin, DeviceStore())
        except exceptions.AuthenticationError as exc:
            logging.getLogger(__name__).info("pairing failed: %s", exc)
            if "cancelled" in str(exc):
                _emit("error", code="cancelled", message="Pairing cancelled")
            elif asked:
                _emit(
                    "error",
                    code="pairing-refused",
                    message=f"{name} did not accept the PIN",
                )
            else:
                # Refused before showing a PIN: a Mac whose "Allow AirPlay
                # for" is "Current User" takes only its owner's devices.
                _emit(
                    "error",
                    code="receiver-restricted",
                    message=f"{name} only accepts its owner's devices. On a Mac, "
                    "set Allow AirPlay for to Anyone on the Same Network",
                )
            return 1
        except Exception as exc:  # noqa: BLE001 - reported to the widget
            logging.getLogger(__name__).exception("pairing failed")
            _emit("error", message=f"{type(exc).__name__}: {exc}")
            return 1
        _emit("paired", id=device_id, name=name)
        return 0

    return asyncio.run(run())


def cmd_unpair(args) -> int:
    _print({"forgotten": DeviceStore().forget(args.id)})
    return 0


def cmd_status(_args) -> int:
    _restore_audio_if_idle()
    owner = _read_owner() or {}
    _print(
        {
            "running": bool(owner),
            "pid": owner.get("pid"),
            "name": owner.get("name"),
            "host": owner.get("host"),
            "id": owner.get("id"),
        }
    )
    return 0


def cmd_stop(_args) -> int:
    pid = _read_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:  # it ended on its own just now
            pid = None
    _print({"stopped": pid is not None})
    return 0


def cmd_firewall(args) -> int:
    _print(firewall.check(args.timing_port))
    return 0


def cmd_mirror(args) -> int:
    # Seen before anything slow (the receiver lookup), so a parent that dies
    # meanwhile is still noticed by die_with_parent.
    parent = os.getppid()
    if (pid := _read_pid()) is not None:
        _emit("error", message=f"already mirroring (pid {pid}); run 'omashare stop'")
        return 1
    try:
        max_w, max_h = (int(v) for v in args.max_size.split("x"))
        device_id, name = _resolve(args)
    except (ValueError, LookupError) as exc:
        _emit("error", message=str(exc))
        return 1
    identity = DeviceStore().identity(device_id)
    if identity is None:
        _emit("error", code="not-paired", id=device_id, message=f"{name} is not paired yet")
        return 2
    lock = _claim_pid_file({**this_process(), "name": name, "host": args.host, "id": device_id})
    if lock is None:
        _emit("error", message="already mirroring; run 'omashare stop'")
        return 1

    async def run() -> int:
        # The widget runs the helper through uv, which a shell restart may kill
        # outright; the share would then go on with no UI to stop it. SIGTERM
        # ends it cleanly: TEARDOWN, and the audio route restored.
        die_with_parent(signal.SIGTERM, parent)
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()

        def stop() -> None:
            # Once: a second signal would cancel the TEARDOWN under way.
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
            task.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop)
        try:
            await mirror.mirror(
                args.host,
                args.port,
                identity=identity,
                monitor=args.monitor,
                fps=args.fps,
                max_size=(max_w, max_h),
                cursor=not args.no_cursor,
                audio_mode=args.audio,
                source=args.source,
                timing_port=args.timing_port,
                emit=_emit,
            )
        except asyncio.CancelledError:
            _emit("ended", reason="stopped")
        except exceptions.AuthenticationError as exc:
            # The receiver no longer knows the stored identity (reset, or the
            # pairing was removed there): it has to be paired again.
            logging.getLogger(__name__).info("media pair-verify refused: %s", exc)
            _emit(
                "error",
                code="not-paired",
                id=device_id,
                message=f"{name} no longer knows this computer; pair again",
            )
            return 2
        except TimeoutError:
            # The receiver accepted the session but answers the stream SETUPs
            # only after a clock sync, which a firewall can block.
            logging.getLogger(__name__).exception("mirror failed")
            _emit(
                "error",
                code="receiver-silent",
                message=f"{name} stopped answering. A firewall may be blocking "
                f"its timing requests to UDP port {args.timing_port}",
            )
            return 1
        except Exception as exc:  # noqa: BLE001 - reported to the widget
            logging.getLogger(__name__).exception("mirror failed")
            if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE:
                _emit(
                    "error",
                    code="timing-port-busy",
                    message=f"UDP port {args.timing_port} is in use; "
                    "choose another timing port in the settings",
                )
            else:
                _emit("error", message=f"{type(exc).__name__}: {exc}")
            return 1
        return 0

    try:
        return asyncio.run(run())
    finally:
        PID_FILE.unlink(missing_ok=True)
        lock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omashare")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug log")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="list AirPlay receivers as JSON")
    p.add_argument("--timeout", type=float, default=3.0)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("pair", help="pair with a receiver (it shows a PIN)")
    p.add_argument("host")
    p.add_argument("--port", type=int, default=7000)
    p.add_argument("--id", help="receiver identifier, as scan reports it")
    p.add_argument("--name", help="receiver name, for messages")
    p.set_defaults(func=cmd_pair)

    p = sub.add_parser("unpair", help="forget the pairing with a receiver")
    p.add_argument("--id", required=True, help="receiver identifier")
    p.set_defaults(func=cmd_unpair)

    p = sub.add_parser("mirror", help="mirror the screen to a receiver")
    p.add_argument("host")
    p.add_argument("--port", type=int, default=7000)
    p.add_argument("--id", help="receiver identifier, as scan reports it")
    p.add_argument("--name", help="receiver name, for messages")
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
    p.add_argument(
        "--timing-port",
        type=int,
        default=DEFAULT_TIMING_PORT,
        help="local UDP port for the receiver's timing requests (0: any free port)",
    )
    p.set_defaults(func=cmd_mirror)

    p = sub.add_parser("firewall", help="can receivers reach the timing port")
    p.add_argument("--timing-port", type=int, default=DEFAULT_TIMING_PORT)
    p.set_defaults(func=cmd_firewall)

    sub.add_parser("status", help="is a mirror running").set_defaults(func=cmd_status)
    sub.add_parser("stop", help="stop the running mirror").set_defaults(func=cmd_stop)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.command in ("mirror", "pair"))
    return args.func(args)


def log_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "omashare" / "omashare.log"


class _PrivateLog(logging.handlers.RotatingFileHandler):
    """A rotating log only this user can read, including files made by rotation."""

    def _open(self):
        stream = open(  # noqa: SIM115 - the handler owns and closes it
            self.baseFilename,
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
            opener=lambda path, flags: os.open(path, flags, 0o600),
        )
        os.chmod(self.baseFilename, 0o600)  # a file from an older version
        return stream


def _setup_logging(verbose: bool, to_file: bool) -> None:
    """Log to stderr (stdout is reserved for JSON) and, for ``mirror`` and
    ``pair``, to a debug log file, since the bar widget does not keep the
    helper's stderr.

    pyatv's own debug output carries key material (pairing keys, stream keys),
    so it is left out unless *verbose*.
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.DEBUG if verbose else logging.INFO)
    stderr.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(stderr)
    logging.getLogger("pyatv").setLevel(logging.NOTSET if verbose else logging.INFO)
    if to_file:
        path = log_file()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        for old in path.parent.glob(path.name + ".*"):
            os.chmod(old, 0o600)
        handler = _PrivateLog(path, maxBytes=2_000_000, backupCount=2)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(fmt)
        root.addHandler(handler)


if __name__ == "__main__":
    sys.exit(main())
