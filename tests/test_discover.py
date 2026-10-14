import asyncio

from omashare import discover

TXT_TV = '"acl=0" "deviceid=9E:4D:0F:34:D4:7E" "features=0x4A7FDFD5,0x3C175FDE" "model=AppleTV11,1"'
TXT_MAC = '"acl=0" "deviceid=F2:79:31:8C:22:62" "features=0x4A7FCFD5,0x38174FDE" "model=Mac15,6"'
TXT_HOMEPOD = '"deviceid=AA:AA:AA:AA:AA:AA" "features=0x4A7FCA00,0xBC354BD0" "model=AudioAccessory5,1"'

AVAHI = "\n".join([
    "+;wlp0s20f3;IPv6;Bedroom;_airplay._tcp;local",
    f"=;wlp0s20f3;IPv6;Bedroom;_airplay._tcp;local;Bedroom.local;fe80::c24:5652:c082:f826;7000;{TXT_TV}",
    f"=;wlp0s20f3;IPv4;Bedroom;_airplay._tcp;local;Bedroom.local;192.168.88.63;7000;{TXT_TV}",
    f"=;wlp0s20f3;IPv4;Mikhail\\226\\128\\153s\\032MacBook\\032Pro;_airplay._tcp;local;mbp.local;192.168.88.34;7000;{TXT_MAC}",
    f"=;wlp0s20f3;IPv4;Kitchen;_airplay._tcp;local;Kitchen.local;192.168.88.40;7000;{TXT_HOMEPOD}",
])


def test_parses_names_models_and_merges_ip_versions():
    devices = discover.parse_avahi(AVAHI)
    assert devices == [
        {"name": "Bedroom", "address": "192.168.88.63", "port": 7000,
         "identifier": "9E:4D:0F:34:D4:7E", "model": "Apple TV 4K (gen 2)"},
        {"name": "Mikhail’s MacBook Pro", "address": "192.168.88.34", "port": 7000,
         "identifier": "F2:79:31:8C:22:62", "model": "Mac15,6"},
    ]


def test_audio_only_receivers_are_left_out():
    assert "Kitchen" not in [d["name"] for d in discover.parse_avahi(AVAHI)]


def test_ipv4_wins_whichever_comes_first():
    lines = AVAHI.splitlines()
    swapped = "\n".join([lines[2], lines[1]])
    assert discover.parse_avahi(swapped)[0]["address"] == "192.168.88.63"


def test_scan_falls_back_to_pyatv_without_avahi(monkeypatch):
    async def no_avahi(timeout):
        return None

    async def from_pyatv(timeout, hosts):
        return [{"name": "Fallback"}]

    monkeypatch.setattr(discover, "_avahi", no_avahi)
    monkeypatch.setattr(discover, "_pyatv", from_pyatv)
    assert asyncio.run(discover.scan()) == [{"name": "Fallback"}]


def test_scan_for_a_host_uses_the_avahi_entry(monkeypatch):
    async def avahi(timeout):
        return discover.parse_avahi(AVAHI)

    async def must_not(timeout, hosts):
        raise AssertionError("pyatv scanner used although avahi knew the host")

    monkeypatch.setattr(discover, "_avahi", avahi)
    monkeypatch.setattr(discover, "_pyatv", must_not)
    found = asyncio.run(discover.scan(hosts=["192.168.88.34"]))
    assert [d["identifier"] for d in found] == ["F2:79:31:8C:22:62"]
