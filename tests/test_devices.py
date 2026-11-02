import json
import stat

from pyatv.protocols.airplay.mirror import MediaIdentity

from omashare.devices import DeviceStore


def test_round_trip_and_case_insensitive_ids(tmp_path):
    store = DeviceStore(tmp_path / "omashare" / "devices.json")
    identity = MediaIdentity.generate()
    store.save("9e:4d:0f:34:d4:7e", "Bedroom", identity)
    assert store.identity("9E:4D:0F:34:D4:7E") == identity
    assert store.paired() == {"9E:4D:0F:34:D4:7E"}


def test_the_file_and_directory_are_private(tmp_path):
    path = tmp_path / "omashare" / "devices.json"
    DeviceStore(path).save("AA", "TV", MediaIdentity.generate())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_unknown_and_corrupt_entries_are_not_paired(tmp_path):
    path = tmp_path / "devices.json"
    path.write_text(json.dumps({"AA": {"identity": "zz"}, "BB": "junk"}))
    store = DeviceStore(path)
    assert store.identity("AA") is None
    assert store.identity("CC") is None
    assert store.paired() == set()


def test_forget(tmp_path):
    store = DeviceStore(tmp_path / "devices.json")
    store.save("AA", "TV", MediaIdentity.generate())
    assert store.forget("aa") is True
    assert store.forget("aa") is False
    assert store.identity("AA") is None
