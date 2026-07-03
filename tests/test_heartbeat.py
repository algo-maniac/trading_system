"""Heartbeat: fires at most once per interval, using an injectable clock (no real sleep)."""
from trading_system.agents.heartbeat import Heartbeat


def test_does_not_fire_before_interval_elapses():
    t = {"now": 0.0}
    hb = Heartbeat(interval_seconds=60.0, clock=lambda: t["now"])
    assert hb.tick("hb", n=1) is False
    t["now"] = 30.0
    assert hb.tick("hb", n=1) is False


def test_fires_once_interval_elapses_then_resets():
    t = {"now": 0.0}
    hb = Heartbeat(interval_seconds=60.0, clock=lambda: t["now"])
    t["now"] = 61.0
    assert hb.tick("hb", n=1) is True
    assert hb.tick("hb", n=1) is False   # immediately after firing: not due again
    t["now"] = 130.0
    assert hb.tick("hb", n=1) is True
