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
    fallback = tmp_path / "omashare-uid"  # not the real /tmp/omashare-<uid>
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(proc, "Path", lambda _: fallback)
    path = proc.runtime_dir()
    assert path == fallback
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


class _FakeLibc:
    def __init__(self):
        self.calls = []

    def prctl(self, *args):
        self.calls.append(args)
        return 0


@pytest.mark.parametrize("parents, killed", [((100, 100), []), ((100, 1), [15])])
def test_die_with_parent(monkeypatch, parents, killed):
    """Sets PDEATHSIG; signals itself if the parent was already gone.

    An orphan is re-parented to init or a subreaper, so the check compares
    with the parent seen before the prctl rather than with pid 1.
    """
    libc = _FakeLibc()
    ppids = iter(parents)
    sent = []
    monkeypatch.setattr(proc.ctypes, "CDLL", lambda *a, **k: libc)
    monkeypatch.setattr(proc.os, "getppid", lambda: next(ppids))
    monkeypatch.setattr(proc.os, "kill", lambda pid, sig: sent.append(sig))
    proc.die_with_parent(15)
    assert libc.calls == [(proc.PR_SET_PDEATHSIG, 15)]
    assert sent == killed
