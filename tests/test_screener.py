"""Volatile-universe screen: ranking, liquidity/observation filters, determinism."""
from trading_system.data.screener import ScreenRow, rank_by_range

CR = 1e7


def _row(sym, rng, turnover_cr=100.0, sessions=60):
    return ScreenRow(symbol=sym, avg_range_pct=rng, median_turnover=turnover_cr * CR, sessions=sessions)


def test_ranks_by_range_descending_top_n():
    rows = [_row("A", 0.02), _row("B", 0.05), _row("C", 0.03), _row("D", 0.04)]
    assert rank_by_range(rows, top_n=2) == ["B", "D"]


def test_turnover_filter_drops_thin_names():
    rows = [_row("THIN", 0.09, turnover_cr=10.0), _row("LIQ", 0.03)]
    assert rank_by_range(rows, top_n=5, min_turnover_cr=50.0) == ["LIQ"]


def test_min_sessions_drops_fresh_listings():
    rows = [_row("IPO", 0.08, sessions=10), _row("OLD", 0.03)]
    assert rank_by_range(rows, top_n=5, min_sessions=40) == ["OLD"]


def test_deterministic_tiebreak_by_symbol():
    rows = [_row("ZZZ", 0.05), _row("AAA", 0.05)]
    assert rank_by_range(rows, top_n=2) == ["AAA", "ZZZ"]
