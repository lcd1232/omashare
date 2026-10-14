import json
import os
import stat

import pytest

from omashare import cli


@pytest.fixture
def restored(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "restore_stale", lambda: calls.append(True))
    return calls


@pytest.fixture
def pid_file(tmp_path, monkeypatch, restored):
    path = tmp_path / "omashare.pid"
    monkeypatch.setattr(cli, "PID_FILE", path)
    return path


def run(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_status_without_pid_file(pid_file, capsys):
    assert run(capsys, "status") == (
        0,
        {"running": False, "pid": None, "name": None, "host": None, "id": None},
    )


def test_status_names_the_receiver_being_shared(pid_file, capsys):
    from omashare.proc import this_process

    pid_file.write_text(json.dumps({**this_process(), "name": "Bedroom", "host": "h", "id": "AA"}))
    code, status = run(capsys, "status")
    assert status["running"] is True
    assert (status["name"], status["host"], status["id"]) == ("Bedroom", "h", "AA")


def test_a_reused_pid_is_not_a_running_mirror(pid_file, capsys):
    pid_file.write_text(json.dumps({"pid": os.getpid(), "start": "1", "name": "Bedroom"}))
    assert run(capsys, "status")[1]["running"] is False


def test_status_ignores_stale_pid(pid_file, capsys):
    pid_file.write_text("999999999")
    assert run(capsys, "status")[1]["running"] is False


def test_stop_signals_running_mirror(pid_file, capsys, monkeypatch):
    pid_file.write_text(str(os.getpid()))
    sent = []
    real_kill = os.kill
    monkeypatch.setattr(
        cli.os, "kill", lambda pid, sig: sent.append(sig) if sig else real_kill(pid, 0)
    )
    assert run(capsys, "stop") == (0, {"stopped": True})
    assert sent == [cli.signal.SIGTERM]


def test_mirror_refuses_second_session(pid_file, capsys):
    pid_file.write_text(str(os.getpid()))
    code, event = run(capsys, "mirror", "192.0.2.1")
    assert code == 1
    assert event["event"] == "error"
    assert "already mirroring" in event["message"]


def test_status_restores_stale_audio_when_idle(pid_file, capsys, restored):
    run(capsys, "status")
    assert restored == [True]


def test_status_leaves_audio_alone_while_mirroring(pid_file, capsys, restored):
    pid_file.write_text(str(os.getpid()))
    run(capsys, "status")
    assert restored == []


@pytest.fixture
def store(tmp_path, monkeypatch):
    from omashare.devices import DeviceStore

    path = tmp_path / "devices.json"
    monkeypatch.setattr(cli, "DeviceStore", lambda: DeviceStore(path))
    return DeviceStore(path)


def run_lines(capsys, *argv):
    code = cli.main(list(argv))
    return code, [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_mirror_refuses_an_unpaired_receiver(pid_file, store, capsys):
    code, events = run_lines(capsys, "mirror", "192.0.2.1", "--id", "AA:BB")
    assert code == 2
    assert events == [
        {"event": "error", "code": "not-paired", "id": "AA:BB",
         "message": "192.0.2.1 is not paired yet"}
    ]
    assert not pid_file.exists()


def test_mirror_rejects_a_bad_max_size_before_writing_a_pid_file(pid_file, store, capsys):
    code, events = run_lines(capsys, "mirror", "192.0.2.1", "--id", "AA", "--max-size", "1920")
    assert code == 1 and events[0]["event"] == "error"
    assert not pid_file.exists()


def test_pair_reads_the_pin_from_stdin_and_stores_the_identity(store, capsys, monkeypatch):
    import io

    from omashare import pairing

    seen = {}

    async def fake_pair(host, port, device_id, name, pin, store_):
        seen["pin"] = await pin()
        from pyatv.protocols.airplay.mirror import MediaIdentity

        store_.save(device_id, name, MediaIdentity.generate())

    monkeypatch.setattr(pairing, "pair", fake_pair)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("0607\n"))
    code, events = run_lines(capsys, "pair", "192.0.2.1", "--id", "AA", "--name", "Bedroom")
    assert code == 0
    assert seen["pin"] == "0607"  # leading zero kept
    assert events == [
        {"event": "pin", "id": "AA", "name": "Bedroom"},
        {"event": "paired", "id": "AA", "name": "Bedroom"},
    ]
    assert store.identity("AA") is not None


def test_pair_reports_a_refused_pin(store, capsys, monkeypatch):
    import io

    from pyatv import exceptions

    from omashare import pairing

    async def refused(host, port, device_id, name, pin, store_):
        await pin()
        raise exceptions.AuthenticationError("media pairing step ['pk', 'proof'] returned HTTP 470")

    monkeypatch.setattr(pairing, "pair", refused)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("1234\n"))
    code, events = run_lines(capsys, "pair", "192.0.2.1", "--id", "AA", "--name", "Bedroom")
    assert code == 1
    assert events[-1] == {"event": "error", "code": "pairing-refused",
                          "message": "Bedroom did not accept the PIN"}
    assert store.identity("AA") is None


def test_pair_reports_a_receiver_that_refuses_before_the_pin(store, capsys, monkeypatch):
    from pyatv import exceptions

    from omashare import pairing

    async def restricted(host, port, device_id, name, pin, store_):
        raise exceptions.AuthenticationError("not authenticated")  # HTTP 403

    monkeypatch.setattr(pairing, "pair", restricted)
    code, events = run_lines(capsys, "pair", "192.0.2.1", "--id", "AA", "--name", "MacBook")
    assert code == 1
    assert events == [{"event": "error", "code": "receiver-restricted",
                       "message": "MacBook only accepts its owner's devices. On a Mac, "
                       "set Allow AirPlay for to Anyone on the Same Network"}]


def test_pair_treats_an_empty_line_as_cancel(store, capsys, monkeypatch):
    import io

    from omashare import pairing

    async def wait_pin(host, port, device_id, name, pin, store_):
        await pin()

    monkeypatch.setattr(pairing, "pair", wait_pin)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    code, events = run_lines(capsys, "pair", "192.0.2.1", "--id", "AA")
    assert code == 1 and events[-1]["code"] == "cancelled"


def test_scan_marks_paired_receivers_and_lists_them_first(store, capsys, monkeypatch):
    from pyatv.protocols.airplay.mirror import MediaIdentity

    from omashare import discover

    async def fake_scan(timeout, hosts=None):
        return [
            {"name": "Attic", "identifier": "DD"},
            {"name": "Bedroom", "identifier": "aa:bb"},
            {"name": "Den", "identifier": "CC"},
            {"name": "Kitchen", "identifier": "EE"},
        ]

    monkeypatch.setattr(discover, "scan", fake_scan)
    store.save("AA:BB", "Bedroom", MediaIdentity.generate())
    store.save("EE", "Kitchen", MediaIdentity.generate())
    code, devices = run(capsys, "scan")
    assert [(d["name"], d["paired"]) for d in devices] == [
        ("Bedroom", True), ("Kitchen", True), ("Attic", False), ("Den", False),
    ]


@pytest.fixture
def paired(store):
    from pyatv.protocols.airplay.mirror import MediaIdentity

    store.save("AA", "MacBook", MediaIdentity.generate())
    return store


def _failing_mirror(monkeypatch, exc, seen=None):
    from omashare import mirror

    async def fake(host, port, **kwargs):
        if seen is not None:
            seen.update(kwargs)
        raise exc

    monkeypatch.setattr(mirror, "mirror", fake)


def test_mirror_uses_the_fixed_timing_port(pid_file, paired, capsys, monkeypatch):
    seen = {}
    _failing_mirror(monkeypatch, RuntimeError("boom"), seen)
    run_lines(capsys, "mirror", "192.0.2.1", "--id", "AA")
    assert seen["timing_port"] == 47010
    run_lines(capsys, "mirror", "192.0.2.1", "--id", "AA", "--timing-port", "5000")
    assert seen["timing_port"] == 5000


def test_mirror_explains_a_receiver_that_stops_answering(pid_file, paired, capsys, monkeypatch):
    _failing_mirror(monkeypatch, TimeoutError("no response to SETUP rtsp://x/1 (RTSP/1.0)"))
    code, events = run_lines(capsys, "mirror", "192.0.2.1", "--id", "AA", "--name", "MacBook")
    assert code == 1
    assert events[-1] == {
        "event": "error",
        "code": "receiver-silent",
        "message": "MacBook stopped answering. A firewall may be blocking "
        "its timing requests to UDP port 47010",
    }


def test_mirror_reports_a_busy_timing_port(pid_file, paired, capsys, monkeypatch):
    import errno

    _failing_mirror(monkeypatch, OSError(errno.EADDRINUSE, "Address already in use"))
    code, events = run_lines(capsys, "mirror", "192.0.2.1", "--id", "AA")
    assert code == 1 and events[-1]["code"] == "timing-port-busy"


def test_mirror_reports_other_os_errors_as_they_are(pid_file, paired, capsys, monkeypatch):
    _failing_mirror(monkeypatch, ConnectionRefusedError(111, "Connection refused"))
    code, events = run_lines(capsys, "mirror", "192.0.2.1", "--id", "AA")
    assert code == 1
    assert "code" not in events[-1]
    assert "Connection refused" in events[-1]["message"]


def test_firewall_command(capsys, monkeypatch):
    from omashare import firewall

    monkeypatch.setattr(firewall, "check", lambda port: {"port": port})
    assert run(capsys, "firewall") == (0, {"port": 47010})
    assert run(capsys, "firewall", "--timing-port", "5000") == (0, {"port": 5000})


def test_the_log_file_is_private_and_keeps_pyatv_secrets_out():
    import logging

    cli._setup_logging(verbose=False, to_file=True)
    logging.getLogger("pyatv.protocols.airplay.srp").debug("Authentication keys (Private=%s)", "ab" * 32)
    logging.getLogger("omashare.mirror").info("audio: routed")
    for handler in logging.getLogger().handlers:
        handler.flush()

    path = cli.log_file()
    text = path.read_text()
    assert "Private=" not in text
    assert "audio: routed" in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_verbose_keeps_pyatv_debug():
    import logging

    cli._setup_logging(verbose=True, to_file=False)
    assert logging.getLogger("pyatv").getEffectiveLevel() == logging.DEBUG
