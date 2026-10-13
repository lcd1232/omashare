import json
import subprocess

import pytest

from omashare.sink import AudioRoute, restore_stale


class FakePactl:
    def __init__(self, default="speakers", inputs="12\t3\t-\tfloat32le\n13\t3\t-\ts16le"):
        self.calls = []
        self.default = default
        self.inputs = inputs
        self.fail = set()

    def __call__(self, args):
        self.calls.append(args)
        if args[0] in self.fail:
            raise subprocess.CalledProcessError(1, ["pactl", *args])
        if args == ["get-default-sink"]:
            return self.default
        if args[0] == "load-module":
            return "536870913"
        if args[:3] == ["list", "short", "sink-inputs"]:
            return self.inputs
        if args[0] == "set-default-sink":
            self.default = args[1]
        return ""


@pytest.fixture
def state(tmp_path):
    return tmp_path / "sink.json"


def test_off_captures_nothing_and_changes_nothing(state):
    pactl = FakePactl()
    with AudioRoute("off", pactl=pactl, state_file=state) as source:
        assert source is None
    assert pactl.calls == []


def test_both_captures_default_monitor_without_rerouting(state):
    pactl = FakePactl()
    with AudioRoute("both", pactl=pactl, state_file=state) as source:
        assert source == "speakers.monitor"
    assert pactl.calls == [["get-default-sink"]]


def test_tv_routes_to_null_sink_and_restores(state):
    pactl = FakePactl()
    with AudioRoute("tv", pactl=pactl, state_file=state) as source:
        assert source == "omashare.monitor"
        assert pactl.default == "omashare"
        assert ["move-sink-input", "12", "omashare"] in pactl.calls
        assert ["move-sink-input", "13", "omashare"] in pactl.calls
        assert json.loads(state.read_text()) == {
            "module": "536870913",
            "previous": "speakers",
        }
    assert pactl.default == "speakers"
    assert pactl.calls[-1] == ["unload-module", "536870913"]
    assert not state.exists()


def test_tv_restores_after_an_exception(state):
    pactl = FakePactl()
    with pytest.raises(RuntimeError):
        with AudioRoute("tv", pactl=pactl, state_file=state):
            raise RuntimeError("session died")
    assert pactl.default == "speakers"
    assert ["unload-module", "536870913"] in pactl.calls


def test_failed_stream_move_does_not_abort(state):
    pactl = FakePactl()
    pactl.fail.add("move-sink-input")
    with AudioRoute("tv", pactl=pactl, state_file=state) as source:
        assert source == "omashare.monitor"


def test_stale_sink_from_a_killed_helper_is_undone(state):
    state.write_text(json.dumps({"module": "77", "previous": "headphones"}))
    pactl = FakePactl(default="omashare")
    restore_stale(pactl, state)
    assert pactl.calls == [["set-default-sink", "headphones"], ["unload-module", "77"]]
    assert not state.exists()


def test_new_route_cleans_up_stale_state_first(state):
    state.write_text(json.dumps({"module": "77", "previous": "headphones"}))
    pactl = FakePactl(default="omashare")
    with AudioRoute("both", pactl=pactl, state_file=state) as source:
        assert source == "headphones.monitor"


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        AudioRoute("loud")
