"""Roster: join/rejoin/beat/peers/reap contract tests."""

import time

from fungi.hub.roster import Roster


def test_join_rejoin_peers():
    roster = Roster()
    assert roster.join("alpha", "1.2.3.4") is True
    assert roster.join("beta", "1.2.3.5") is True
    assert roster.join("alpha", "1.2.3.4") is False  # rejoin is not new
    assert roster.peers("alpha") == ["beta"]
    assert roster.peers("beta") == ["alpha"]
    assert roster.peers("ghost") == ["alpha", "beta"]


def test_beat_and_leave():
    roster = Roster()
    roster.join("alpha", "x")
    assert roster.beat("alpha") is True
    assert roster.beat("ghost") is False
    assert roster.leave("alpha") is True
    assert roster.leave("alpha") is False
    assert roster.beat("alpha") is False


def test_reap_timeout():
    roster = Roster(heartbeat_timeout=0.05)
    roster.join("alpha", "x")
    roster.join("beta", "y")
    time.sleep(0.1)
    roster.beat("beta")  # beta stays fresh; alpha went silent
    assert roster.reap() == ["alpha"]
    assert roster.known("beta") is True
