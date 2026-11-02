"""Does the host firewall let receivers reach the timing port?

While mirroring, the receiver sends clock-sync requests to a UDP port on
this computer and waits for them before it answers the stream SETUPs. Every
other connection is opened from here. Omarchy turns ufw on with incoming
traffic denied, so that port needs one allow rule.

ufw's configuration files are world-readable, so this needs no root.
Other firewalls are not inspected: a blocked port then shows up as a
receiver that never answers (see ``cli``).
"""

from __future__ import annotations

import re
from pathlib import Path

#: The UDP port receivers send timing requests to, unless configured.
DEFAULT_TIMING_PORT = 47010

UFW_CONF = Path("/etc/ufw/ufw.conf")
UFW_DEFAULTS = Path("/etc/default/ufw")
UFW_RULES = Path("/etc/ufw/user.rules")

_ANY_SOURCE = "0.0.0.0/0"


def allow_command(port: int) -> str:
    """The command that opens *port*, to be run by the user."""
    return f"sudo ufw allow {port}/udp comment omashare"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _value(text: str, key: str) -> str:
    match = re.search(rf"^\s*{key}\s*=\s*\"?([^\"\n]*)\"?", text, re.MULTILINE)
    return match.group(1).strip().lower() if match else ""


def _port_in(spec: str, port: int) -> bool:
    """*port* is in a ufw port spec: ``47010``, ``47000:47100`` or a list."""
    for part in spec.split(","):
        low, _, high = part.partition(":")
        try:
            if int(low) <= port <= int(high or low):
                return True
        except ValueError:
            continue
    return False


def _allows(rules: str, port: int) -> bool:
    """A rule lets UDP *port* in.

    A rule for the port counts whatever its source, since that is the
    user's choice. A rule for every port counts only from any source: a
    rule that lets one receiver in on every port says nothing about others.
    """
    for line in rules.splitlines():
        if not line.startswith("### tuple ###"):
            continue
        fields = line.split()[3:]
        if len(fields) < 7:
            continue
        action, proto, dport, _dst, _sport, src, direction = fields[:7]
        if not action.startswith("allow") or not direction.startswith("in"):
            continue
        if proto not in ("udp", "any"):
            continue
        if dport == "any" and src == _ANY_SOURCE:
            return True
        if dport != "any" and _port_in(dport, port):
            return True
    return False


def check(
    port: int,
    *,
    conf: Path = UFW_CONF,
    defaults: Path = UFW_DEFAULTS,
    rules: Path = UFW_RULES,
) -> dict:
    """``{"firewall", "allowed", "port", "command"}`` for the widget.

    ``firewall`` is ``"ufw"`` when ufw is on and drops incoming traffic,
    else ``"none"``; ``allowed`` says whether receivers can reach *port*.
    """
    result = {"firewall": "none", "allowed": True, "port": port, "command": ""}
    conf_text = _read(conf)
    if conf_text is None or _value(conf_text, "ENABLED") != "yes":
        return result
    if _value(_read(defaults) or "", "DEFAULT_INPUT_POLICY") == "accept":
        return result
    result["firewall"] = "ufw"
    result["command"] = allow_command(port)
    rules_text = _read(rules)
    # Unreadable rules: don't nag, the receiver timeout still explains it.
    result["allowed"] = rules_text is None or _allows(rules_text, port)
    return result
