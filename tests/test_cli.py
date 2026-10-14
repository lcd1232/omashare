import json
import os

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


def test_pair_treats_an_empty_line_as_cancel(store, capsys, monkeypatch):
    import io

    from omashare import pairing

    async def wait_pin(host, port, device_id, name, pin, store_):
        await pin()

    monkeypatch.setattr(pairing, "pair", wait_pin)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    code, events = run_lines(capsys, "pair", "192.0.2.1", "--id", "AA")
    assert code == 1 and events[-1]["code"] == "cancelled"


def test_scan_marks_paired_receivers(store, capsys, monkeypatch):
    from pyatv.protocols.airplay.mirror import MediaIdentity

    from omashare import discover

    async def fake_scan(timeout, hosts=None):
        return [{"name": "Bedroom", "identifier": "aa:bb"}, {"name": "Den", "identifier": "CC"}]

    monkeypatch.setattr(discover, "scan", fake_scan)
    store.save("AA:BB", "Bedroom", MediaIdentity.generate())
    code, devices = run(capsys, "scan")
    assert [d["paired"] for d in devices] == [True, False]
