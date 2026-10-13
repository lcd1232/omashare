import logging

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep the helper's log file out of the real ~/.local/state, and drop
    the log handlers each ``cli.main()`` call adds to the root logger."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in root.handlers[:]:
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
