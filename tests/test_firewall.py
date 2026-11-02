import pytest

from omashare import firewall

OMARCHY_RULES = """\
### tuple ### allow udp 53317 0.0.0.0/0 any 0.0.0.0/0 in
### tuple ### allow tcp 53317 0.0.0.0/0 any 0.0.0.0/0 in
### tuple ### allow udp 53 172.17.0.1 any 172.16.0.0/12 in comment=616c6c6f772d646f636b65722d646e73
### tuple ### allow any any 0.0.0.0/0 any 192.168.88.63 in
"""


@pytest.fixture
def ufw(tmp_path):
    conf, defaults, rules = (tmp_path / n for n in ("ufw.conf", "default", "user.rules"))
    conf.write_text("# comment\nENABLED=yes\nLOGLEVEL=low\n")
    defaults.write_text('IPV6=yes\nDEFAULT_INPUT_POLICY="DROP"\n')
    rules.write_text(OMARCHY_RULES)

    def check(port=47010):
        return firewall.check(port, conf=conf, defaults=defaults, rules=rules)

    check.conf, check.defaults, check.rules = conf, defaults, rules
    return check


def test_omarchy_default_rules_block_the_port(ufw):
    # The rule for one receiver on every port does not count for others.
    assert ufw() == {
        "firewall": "ufw",
        "allowed": False,
        "port": 47010,
        "command": "sudo ufw allow 47010/udp comment omashare",
    }


@pytest.mark.parametrize(
    "rule",
    [
        "allow udp 47010 0.0.0.0/0 any 0.0.0.0/0 in comment=6f6d6173686172",
        "allow any 47010 0.0.0.0/0 any 0.0.0.0/0 in",
        "allow udp 47000:47100 0.0.0.0/0 any 0.0.0.0/0 in",
        "allow udp 80,47010 0.0.0.0/0 any 0.0.0.0/0 in",
        "allow udp 47010 0.0.0.0/0 any 192.168.0.0/16 in",  # LAN only: user's choice
        "allow any any 0.0.0.0/0 any 0.0.0.0/0 in",
        "allow-log udp 47010 0.0.0.0/0 any 0.0.0.0/0 in",
    ],
)
def test_rules_that_let_the_port_in(ufw, rule):
    ufw.rules.write_text(OMARCHY_RULES + f"### tuple ### {rule}\n")
    assert ufw()["allowed"] is True


@pytest.mark.parametrize(
    "rule",
    [
        "allow tcp 47010 0.0.0.0/0 any 0.0.0.0/0 in",
        "deny udp 47010 0.0.0.0/0 any 0.0.0.0/0 in",
        "allow udp 47010 0.0.0.0/0 any 0.0.0.0/0 out",
        "allow udp 47011 0.0.0.0/0 any 0.0.0.0/0 in",
    ],
)
def test_rules_that_do_not(ufw, rule):
    ufw.rules.write_text(OMARCHY_RULES + f"### tuple ### {rule}\n")
    assert ufw()["allowed"] is False


def test_a_configured_port_is_checked(ufw):
    ufw.rules.write_text(OMARCHY_RULES + "### tuple ### allow udp 5000 0.0.0.0/0 any 0.0.0.0/0 in\n")
    assert ufw(5000)["allowed"] is True
    assert ufw(5000)["command"] == "sudo ufw allow 5000/udp comment omashare"


def test_ufw_turned_off(ufw):
    ufw.conf.write_text("ENABLED=no\n")
    assert ufw() == {"firewall": "none", "allowed": True, "port": 47010, "command": ""}


def test_ufw_accepting_incoming_traffic(ufw):
    ufw.defaults.write_text('DEFAULT_INPUT_POLICY="ACCEPT"\n')
    assert ufw()["firewall"] == "none"


def test_no_ufw(tmp_path):
    missing = tmp_path / "missing"
    result = firewall.check(47010, conf=missing, defaults=missing, rules=missing)
    assert result["firewall"] == "none" and result["allowed"] is True


def test_unreadable_rules_are_not_reported_as_blocked(ufw):
    ufw.rules.unlink()
    assert ufw() == {
        "firewall": "ufw",
        "allowed": True,
        "port": 47010,
        "command": "sudo ufw allow 47010/udp comment omashare",
    }
