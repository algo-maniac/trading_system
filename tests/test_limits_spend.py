"""MonthlySpend: micro-dollar accounting, cap precheck, alert-once — on fakeredis."""
import fakeredis
import pytest

from trading_system.net.limits import BudgetExhausted, MonthlySpend


def _spend(cap=2.0, alert=1.5):
    r = fakeredis.FakeRedis(decode_responses=True)
    return MonthlySpend("llm", cap_usd=cap, alert_usd=alert, client=r)


def test_add_and_total_micro_dollar_accounting():
    s = _spend()
    assert s.total_usd() == 0.0
    total = s.add_usd(0.0025)
    assert total == pytest.approx(0.0025)
    total = s.add_usd(0.0031)
    assert total == pytest.approx(0.0056)
    assert s.total_usd() == pytest.approx(0.0056)


def test_precheck_raises_once_cap_reached():
    s = _spend(cap=0.01)
    s.precheck()                      # under cap — fine
    s.add_usd(0.011)
    with pytest.raises(BudgetExhausted):
        s.precheck()


def test_precheck_allows_overshoot_of_final_call():
    """The cap is checked BEFORE a call; a call in flight may overshoot by its own cost."""
    s = _spend(cap=0.01)
    s.add_usd(0.009)
    s.precheck()                      # 0.009 < 0.01 → the next call is allowed
    s.add_usd(0.005)                  # overshoot recorded faithfully
    assert s.total_usd() == pytest.approx(0.014)
    with pytest.raises(BudgetExhausted):
        s.precheck()


def test_alert_threshold_fires_exactly_once():
    s = _spend(cap=5.0, alert=0.01)
    assert s.crossed_alert_threshold(0.005) is False   # below the line
    assert s.crossed_alert_threshold(0.02) is True     # first crossing
    assert s.crossed_alert_threshold(0.03) is False    # never again this month


def test_alert_disabled_when_threshold_zero():
    s = _spend(alert=0.0)
    assert s.crossed_alert_threshold(100.0) is False


def test_negative_cost_never_decrements():
    s = _spend()
    s.add_usd(0.01)
    s.add_usd(-5.0)   # defensive: bad usage data must not refund the budget
    assert s.total_usd() == pytest.approx(0.01)
