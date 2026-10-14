import os
import stat

import pytest

from omashare import proc


def test_start_time_identifies_this_process():
    assert proc.start_time(os.getpid())
    assert proc.is_running(os.getpid(), proc.start_time(os.getpid()))
    assert not proc.is_running(os.getpid(), "1")
    assert not proc.is_running(None)


def test_fallback_runtime_dir_is_private(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = proc.runtime_dir()
    info = os.lstat(path)
    assert info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) & 0o077 == 0


def test_fallback_runtime_dir_refuses_a_shared_directory(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    os.chmod(shared, 0o777)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(proc, "Path", lambda _: shared)
    with pytest.raises(RuntimeError):
        proc.runtime_dir()
