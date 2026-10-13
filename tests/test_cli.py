import json
import os

import pytest

from omashare import cli


@pytest.fixture
def pid_file(tmp_path, monkeypatch):
    path = tmp_path / "omashare.pid"
    monkeypatch.setattr(cli, "PID_FILE", path)
    return path


def run(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_status_without_pid_file(pid_file, capsys):
    assert run(capsys, "status") == (0, {"running": False, "pid": None})


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
