"""Intraday focus universe: three-tier degrade (focus_list → live screen → static
fallback), held-first + cap, and the guarantee that build_focus_universe NEVER raises
and NEVER returns empty. Regression for the un-renewed-token morning."""
from datetime import date
from unittest.mock import patch

from trading_system.agents.reader import live_app
from trading_system.agents.reader.live_app import _assemble_focus, build_focus_universe

TODAY = date(2026, 7, 6)
FALLBACK = ["ATGL", "VEDL", "BHEL", "RELIANCE"]


# --- pure assembler -------------------------------------------------------------

def test_assemble_uses_focus_when_present():
    ordered, used = _assemble_focus(["AAA", "BBB"], held=[], fallback=FALLBACK)
    assert ordered == ["AAA", "BBB"] and used is False


def test_assemble_drops_to_fallback_when_focus_empty():
    ordered, used = _assemble_focus([], held=[], fallback=FALLBACK)
    assert ordered == FALLBACK and used is True


def test_assemble_held_first_and_deduped():
    ordered, used = _assemble_focus(["AAA", "HELD1"], held=["HELD1", "HELD2"],
                                    fallback=FALLBACK)
    assert ordered == ["HELD1", "HELD2", "AAA"] and used is False


def test_assemble_held_survives_cap():
    ordered, _ = _assemble_focus([f"S{i}" for i in range(60)], held=["HELD1"],
                                 fallback=FALLBACK, cap=50)
    assert len(ordered) == 50 and ordered[0] == "HELD1"


def test_assemble_fallback_still_unions_held():
    ordered, used = _assemble_focus([], held=["HELD1"], fallback=FALLBACK)
    assert ordered[0] == "HELD1" and "ATGL" in ordered and used is True


# --- build_focus_universe (DB seams mocked) -------------------------------------

class _Ledger:
    def __init__(self, held=()):
        self._held = held

    def open_positions(self):
        return [type("P", (), {"symbol": s})() for s in self._held]


def _patches(*, focus_list, screen, held=(), screen_raises=False, conn_raises=False):
    import contextlib

    @contextlib.contextmanager
    def fake_conn():
        if conn_raises:
            raise RuntimeError("db down")
        yield object()

    def fake_screen(conn, pool, today, **kw):
        if screen_raises:
            raise RuntimeError("screen boom")
        return screen

    return [
        patch.object(live_app, "get_connection", fake_conn),
        patch.object(live_app, "volatile_universe", fake_screen),
        patch("trading_system.data.news_store.read_focus_list",
              return_value=focus_list),
        patch("trading_system.execution.ledger.PostgresLedger",
              return_value=_Ledger(held)),
    ]


def _run(**kw):
    alerts = []
    ps = _patches(**kw)
    with ps[0], ps[1], ps[2], ps[3], \
         patch.object(live_app, "RedisBarBus") as bus:
        bus.return_value.publish_alert = lambda k, d: alerts.append(k)
        result = build_focus_universe(TODAY)
    return result, alerts


def test_focus_list_present_is_used():
    result, alerts = _run(focus_list=["NEWS1", "GAP1"], screen=[])
    assert result[:2] == ["NEWS1", "GAP1"] and alerts == []


def test_empty_focus_list_falls_to_live_screen():
    result, alerts = _run(focus_list=[], screen=["VOL1", "VOL2"])
    assert "VOL1" in result and alerts == []          # screen tier, no static fallback


def test_both_empty_uses_static_fallback_and_alerts():
    result, alerts = _run(focus_list=[], screen=[])
    from trading_system.config import get_settings
    assert result == get_settings().intraday_fallback_symbols[:live_app._MAX_FOCUS]
    assert alerts == ["focus_static_fallback"]


def test_screen_exception_degrades_to_static_fallback():
    result, alerts = _run(focus_list=[], screen=[], screen_raises=True)
    assert alerts == ["focus_static_fallback"] and len(result) > 0


def test_db_outage_returns_static_fallback_without_raising():
    result, alerts = _run(focus_list=[], screen=[], conn_raises=True)
    # No held (DB was unreachable) but the static list still comes back + alert fires.
    from trading_system.config import get_settings
    assert result == get_settings().intraday_fallback_symbols[:live_app._MAX_FOCUS]
    assert alerts == ["focus_static_fallback"]


def test_never_empty_never_raises_across_all_paths():
    for kw in ({"focus_list": ["A"], "screen": []},
               {"focus_list": [], "screen": ["V"]},
               {"focus_list": [], "screen": []},
               {"focus_list": [], "screen": [], "screen_raises": True},
               {"focus_list": [], "screen": [], "conn_raises": True}):
        result, _ = _run(**kw)
        assert result, f"empty focus for {kw}"


def test_config_fallback_constant_is_nonempty_and_deduped():
    from trading_system.config import INTRADAY_FALLBACK_SYMBOLS
    assert INTRADAY_FALLBACK_SYMBOLS
    assert len(INTRADAY_FALLBACK_SYMBOLS) == len(set(INTRADAY_FALLBACK_SYMBOLS))
