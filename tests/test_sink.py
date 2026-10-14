import json
import os
import subprocess

import pytest

from omashare.sink import AudioRoute, restore_stale


class FakePactl:
    def __init__(self, default="speakers", inputs="12\t3\t-\tfloat32le\n13\t3\t-\ts16le", sinks=None):
        self.calls = []
        self.default = default
        self.inputs = inputs
        self.sinks = sinks or ["speakers"]
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
        if args[:3] == ["list", "short", "sinks"]:
            return "\n".join(f"{i}\t{n}\tPipeWire\tfloat32le 2ch 48000Hz\tIDLE" for i, n in enumerate(self.sinks))
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
    assert pactl.calls == [["get-default-sink"], ["list", "short", "sinks"]]


def test_tv_routes_to_null_sink_and_restores(state):
    pactl = FakePactl()
    with AudioRoute("tv", pactl=pactl, state_file=state) as source:
        assert source == "omashare.monitor"
        assert pactl.default == "omashare"
        assert ["move-sink-input", "12", "omashare"] in pactl.calls
        assert ["move-sink-input", "13", "omashare"] in pactl.calls
        saved = json.loads(state.read_text())
        assert {k: saved[k] for k in ("module", "previous", "pid")} == {
            "module": "536870913",
            "previous": "speakers",
            "pid": os.getpid(),
        }
        assert saved["start"]  # with the pid, names this exact process
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
    assert pactl.calls == [
        ["get-default-sink"],
        ["set-default-sink", "headphones"],
        ["unload-module", "77"],
    ]
    assert not state.exists()


def test_stale_cleanup_keeps_an_output_the_user_picked_since(state):
    state.write_text(json.dumps({"module": "77", "previous": "speakers"}))
    pactl = FakePactl(default="headphones")
    restore_stale(pactl, state)
    assert ["set-default-sink", "speakers"] not in pactl.calls
    assert ["unload-module", "77"] in pactl.calls


def test_a_reused_pid_is_not_the_owner(state):
    state.write_text(json.dumps(
        {"module": "77", "previous": "speakers", "pid": os.getpid(), "start": "1"}
    ))
    pactl = FakePactl(default="omashare")
    restore_stale(pactl, state)
    assert ["unload-module", "77"] in pactl.calls


def test_a_failure_halfway_through_setup_undoes_the_route(state):
    pactl = FakePactl()
    pactl.fail.add("set-default-sink")
    with pytest.raises(subprocess.CalledProcessError):
        with AudioRoute("tv", pactl=pactl, state_file=state):
            pass
    assert ["unload-module", "536870913"] in pactl.calls
    assert not state.exists()


def test_streams_already_on_easyeffects_stay_there(state):
    pactl = FakePactl(
        sinks=["speakers", "easyeffects_sink"],
        inputs="12\t1\t-\tfloat32le\n13\t0\t-\ts16le",
    )
    with AudioRoute("tv", pactl=pactl, state_file=state):
        assert ["move-sink-input", "13", "omashare"] in pactl.calls
        assert ["move-sink-input", "12", "omashare"] not in pactl.calls


def test_new_route_cleans_up_stale_state_first(state):
    state.write_text(json.dumps({"module": "77", "previous": "headphones"}))
    pactl = FakePactl(default="omashare")
    with AudioRoute("both", pactl=pactl, state_file=state) as source:
        assert source == "headphones.monitor"


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        AudioRoute("loud")


def test_sink_of_a_running_owner_is_not_stale(state):
    state.write_text(json.dumps({"module": "77", "previous": "headphones", "pid": os.getpid()}))
    pactl = FakePactl(default="omashare")
    restore_stale(pactl, state)
    assert pactl.calls == []
    assert state.exists()


def test_sink_of_a_dead_owner_is_restored(state):
    dead = subprocess.Popen(["true"])
    dead.wait()
    state.write_text(json.dumps({"module": "77", "previous": "headphones", "pid": dead.pid}))
    pactl = FakePactl(default="omashare")
    restore_stale(pactl, state)
    assert ["unload-module", "77"] in pactl.calls
    assert not state.exists()


@pytest.mark.parametrize("mode", ["tv", "both"])
def test_easyeffects_input_is_captured_before_its_effects(state, mode):
    pactl = FakePactl(sinks=["speakers", "easyeffects_sink"])
    with AudioRoute(mode, pactl=pactl, state_file=state) as source:
        assert source == "easyeffects_sink.monitor"
        if mode == "tv":
            assert pactl.default == "omashare"


def test_effects_are_kept_when_asked(state):
    pactl = FakePactl(sinks=["speakers", "easyeffects_sink"])
    with AudioRoute("tv", bypass_effects=False, pactl=pactl, state_file=state) as source:
        assert source == "omashare.monitor"


def test_streams_on_another_output_go_back_to_it(state):
    # Spotify (12) was routed to USB headphones; Firefox (13) followed the default.
    pactl = FakePactl(sinks=["speakers", "headphones"], inputs="12\t1\t-\tfloat32le\n13\t0\t-\ts16le")
    with AudioRoute("tv", pactl=pactl, state_file=state):
        assert json.loads(state.read_text())["pinned"] == {"12": "headphones"}
        assert ["move-sink-input", "12", "omashare"] in pactl.calls
        assert ["move-sink-input", "13", "omashare"] in pactl.calls
        moves_before = len(pactl.calls)
    after = pactl.calls[moves_before:]
    assert ["move-sink-input", "12", "headphones"] in after
    assert not any(c[:2] == ["move-sink-input", "13"] for c in after)  # the default takes it
    assert after.index(["move-sink-input", "12", "headphones"]) < after.index(["unload-module", "536870913"])


def test_stale_cleanup_moves_pinned_streams_back(state):
    state.write_text(json.dumps({"module": "77", "previous": "speakers", "pinned": {"12": "headphones"}}))
    pactl = FakePactl(default="omashare")
    restore_stale(pactl, state)
    assert pactl.calls == [
        ["get-default-sink"],
        ["move-sink-input", "12", "headphones"],
        ["set-default-sink", "speakers"],
        ["unload-module", "77"],
    ]
