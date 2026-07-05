"""ReporterCore: data-driven daily/weekly/monthly report generation.

"Data-driven" is the load-bearing idea (user requirement): a period is reported when
the LEDGER shows a later period exists — never on wall-clock. A daily report for day
D is due once any equity snapshot lands on a date after D; a week is due once a
snapshot lands in a later ISO week; a month likewise. This makes the same code serve
the live system (a report appears the morning after each session) and the replay
namespace (where months of data-time pass in minutes).

All reads are injected callables (AnalystCore's testing discipline) so the whole
period/report logic is unit-testable without a database. app.py wires the SQL from
repo.py; tests wire dicts.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from trading_system.analytics.metrics import max_drawdown, sharpe_ratio
from trading_system.observability import get_logger

logger = get_logger(__name__)

# Baseline lookback: how far before period_start to search for the "previous" equity
# point (the return baseline). 10 calendar days spans any NSE holiday cluster.
_BASELINE_LOOKBACK_DAYS = 10


@dataclass(frozen=True)
class Period:
    period_type: str   # daily | weekly | monthly
    start: date
    end: date


def _iso_week_bounds(d: date) -> tuple[date, date]:
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def _month_bounds(d: date) -> tuple[date, date]:
    start = d.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start, end


def pending_periods(snapshot_dates: list[date],
                    reported: set[tuple[str, date]]) -> list[Period]:
    """Periods whose data is complete (a later date exists) and not yet reported.

    Ordered oldest-first, dailies before weeklies before monthlies for a given span,
    so a fresh namespace backfills its whole report history deterministically.
    """
    if not snapshot_dates:
        return []
    dates = sorted(set(snapshot_dates))
    latest = dates[-1]
    out: list[Period] = []

    for d in dates:
        if d < latest and ("daily", d) not in reported:
            out.append(Period("daily", d, d))

    latest_week = _iso_week_bounds(latest)[0]
    for monday in sorted({_iso_week_bounds(d)[0] for d in dates}):
        if monday < latest_week and ("weekly", monday) not in reported:
            out.append(Period("weekly", monday, monday + timedelta(days=6)))

    latest_month = latest.replace(day=1)
    for first in sorted({d.replace(day=1) for d in dates}):
        if first < latest_month and ("monthly", first) not in reported:
            out.append(Period("monthly", first, _month_bounds(first)[1]))

    return out


def period_for(period_type: str, d: date) -> Period:
    """Resolve the period containing `d` (the CLI's --period/--date entry point)."""
    if period_type == "daily":
        return Period("daily", d, d)
    if period_type == "weekly":
        start, end = _iso_week_bounds(d)
        return Period("weekly", start, end)
    if period_type == "monthly":
        start, end = _month_bounds(d)
        return Period("monthly", start, end)
    raise ValueError(f"unknown period type '{period_type}'")


@dataclass
class ReporterDeps:
    """Injected reads. Every callable takes (from_date, to_date) IST-date bounds
    unless noted. app.py wires repo.py; tests wire fakes."""
    # [(ist_date, sleeve, equity)] ordered by ts — one row per snapshot per sleeve
    equity_rows: Callable[[date, date], list] = None            # type: ignore[assignment]
    # [{sleeve, symbol, qty, avg_price, realized_pnl, opened_ts, closed_ts, exit_reason}]
    closed_positions: Callable[[date, date], list] = None       # type: ignore[assignment]
    total_fees: Callable[[date, date], float] = None            # type: ignore[assignment]
    # [{sleeve, symbol, qty, avg_price, opened_ts}] — open NOW (approximates period end)
    open_positions: Callable[[], list] = field(default=None)    # type: ignore[assignment]
    signal_count: Callable[[date, date], int] = None            # type: ignore[assignment]
    order_count: Callable[[date, date], int] = None             # type: ignore[assignment]
    fill_count: Callable[[date, date], int] = None              # type: ignore[assignment]
    admissions: Callable[[date, date], list] = field(default=None)  # type: ignore[assignment]
    news_count: Callable[[date, date], int] = None              # type: ignore[assignment]
    # () -> {"llm_month_usd": float|None, "stockinsights_today": int|None} best-effort
    budget_snapshot: Callable[[], dict] = field(default=None)   # type: ignore[assignment]


class ReporterCore:
    def __init__(self, deps: ReporterDeps, namespace: str = "live") -> None:
        self.d = deps
        self.namespace = namespace

    # --- report assembly ---------------------------------------------------------

    def build_report(self, period: Period) -> tuple[dict, str]:  # type: ignore[type-arg]
        """Compute the payload dict and render its markdown for one period."""
        rows = self.d.equity_rows(period.start - timedelta(days=_BASELINE_LOOKBACK_DAYS),
                                  period.end)
        book_by_date, sleeves_by_date = self._book_curves(rows)
        in_period = [(dt, eq) for dt, eq in book_by_date if period.start <= dt <= period.end]
        baseline = self._baseline(book_by_date, period.start)

        equity_start = baseline if baseline is not None else (
            in_period[0][1] if in_period else None)
        equity_end = in_period[-1][1] if in_period else None
        period_return = ((equity_end / equity_start - 1.0)
                         if equity_start and equity_end else None)

        curve = [eq for _, eq in in_period]
        max_dd = max_drawdown(curve) if len(curve) >= 2 else 0.0
        daily_returns = [curve[i] / curve[i - 1] - 1.0 for i in range(1, len(curve))
                         if curve[i - 1]]
        sharpe = (sharpe_ratio(daily_returns, 252)
                  if period.period_type != "daily" and len(daily_returns) >= 2 else None)

        sleeve_summary = {}
        for sleeve, by_date in sleeves_by_date.items():
            sp = [(dt, eq) for dt, eq in by_date if period.start <= dt <= period.end]
            sb = self._baseline(by_date, period.start)
            s_start = sb if sb is not None else (sp[0][1] if sp else None)
            s_end = sp[-1][1] if sp else None
            sleeve_summary[sleeve] = {
                "equity_end": s_end,
                "return": (s_end / s_start - 1.0) if s_start and s_end else None,
            }

        closed = self.d.closed_positions(period.start, period.end)
        trade_stats = self._trade_stats(closed)
        open_pos = self.d.open_positions() if self.d.open_positions else []

        payload = {
            "namespace": self.namespace,
            "period_type": period.period_type,
            "period_start": period.start.isoformat(),
            "period_end": period.end.isoformat(),
            "sessions": len(in_period),
            "equity_start": equity_start,
            "equity_end": equity_end,
            "period_return": period_return,
            "max_drawdown": max_dd,
            "sharpe": sharpe,
            "sleeves": sleeve_summary,
            "trades": trade_stats,
            "fees": self.d.total_fees(period.start, period.end),
            "open_positions": open_pos,
            "activity": {
                "signals": self.d.signal_count(period.start, period.end),
                "orders": self.d.order_count(period.start, period.end),
                "fills": self.d.fill_count(period.start, period.end),
            },
            "admissions": self.d.admissions(period.start, period.end)
                          if self.d.admissions else [],
            "news_items": self.d.news_count(period.start, period.end)
                          if self.d.news_count else 0,
            "budgets": self.d.budget_snapshot() if self.d.budget_snapshot else {},
        }
        return payload, render_markdown(payload)

    # --- internals -----------------------------------------------------------------

    @staticmethod
    def _book_curves(rows: list) -> tuple[list, dict]:  # type: ignore[type-arg]
        """(ist_date, sleeve, equity) rows → book curve (sleeves summed per date, using
        each sleeve's LAST snapshot that date) + per-sleeve curves."""
        last: dict[tuple[date, str], float] = {}
        for dt, sleeve, equity in rows:
            last[(dt, sleeve)] = float(equity)   # rows ordered by ts → last write wins
        dates = sorted({dt for dt, _ in last})
        sleeves = sorted({s for _, s in last})
        carry: dict[str, float] = {}
        book: list[tuple[date, float]] = []
        per_sleeve: dict[str, list[tuple[date, float]]] = {s: [] for s in sleeves}
        for dt in dates:
            for s in sleeves:
                if (dt, s) in last:
                    carry[s] = last[(dt, s)]
                if s in carry:
                    per_sleeve[s].append((dt, carry[s]))
            book.append((dt, sum(carry.values())))
        return book, per_sleeve

    @staticmethod
    def _baseline(by_date: list, start: date) -> float | None:  # type: ignore[type-arg]
        """Last equity strictly before the period — the return baseline."""
        prior = [eq for dt, eq in by_date if dt < start]
        return prior[-1] if prior else None

    @staticmethod
    def _trade_stats(closed: list) -> dict:  # type: ignore[type-arg]
        pnls = [float(p["realized_pnl"]) for p in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        reasons: dict[str, int] = {}
        for p in closed:
            r = p.get("exit_reason") or "unknown"
            reasons[r] = reasons.get(r, 0) + 1
        gross_win, gross_loss = sum(wins), abs(sum(losses))
        return {
            "count": len(pnls),
            "realized_pnl": sum(pnls),
            "win_rate": len(wins) / len(pnls) if pnls else None,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
            "avg_win": gross_win / len(wins) if wins else None,
            "avg_loss": gross_loss / len(losses) if losses else None,
            "exit_reasons": dict(sorted(reasons.items())),
        }


# --- markdown ----------------------------------------------------------------------

def _pct(x: "float | None") -> str:
    return f"{x * 100:+.2f}%" if x is not None else "n/a"


def _inr(x: "float | None") -> str:
    return f"₹{x:,.0f}" if x is not None else "n/a"


def render_markdown(p: dict) -> str:  # type: ignore[type-arg]
    """Deterministic, compact markdown for one report payload."""
    t = p["trades"]
    lines = [
        f"# {p['period_type'].capitalize()} report — {p['period_start']}"
        + (f" → {p['period_end']}" if p['period_end'] != p['period_start'] else ""),
        "",
        f"Namespace: `{p['namespace']}` · sessions: {p['sessions']}",
        "",
        "## Book",
        f"- Equity: {_inr(p['equity_start'])} → {_inr(p['equity_end'])} "
        f"({_pct(p['period_return'])})",
        f"- Max drawdown (within period): {_pct(p['max_drawdown'])}",
    ]
    if p.get("sharpe") is not None:
        lines.append(f"- Sharpe (daily, annualized): {p['sharpe']:.2f}")
    lines += ["", "## Sleeves"]
    for name, s in sorted(p["sleeves"].items()):
        lines.append(f"- `{name}`: {_inr(s['equity_end'])} ({_pct(s['return'])})")
    lines += [
        "",
        "## Trades (closed in period)",
        f"- Count: {t['count']} · realized PnL: {_inr(t['realized_pnl'])} "
        f"· fees: {_inr(p['fees'])}",
    ]
    if t["count"]:
        wr = f"{t['win_rate'] * 100:.0f}%" if t["win_rate"] is not None else "n/a"
        pf = f"{t['profit_factor']:.2f}" if t["profit_factor"] is not None else "n/a"
        lines.append(f"- Win rate: {wr} · profit factor: {pf} "
                     f"· avg win {_inr(t['avg_win'])} / avg loss {_inr(t['avg_loss'])}")
        reasons = ", ".join(f"{k}: {v}" for k, v in t["exit_reasons"].items())
        lines.append(f"- Exits: {reasons}")
    lines += [
        "",
        "## Open positions (as of generation)",
    ]
    if p["open_positions"]:
        for pos in p["open_positions"]:
            lines.append(f"- `{pos['sleeve']}` {pos['symbol']} ×{pos['qty']} "
                         f"@ {_inr(pos['avg_price'])} (since {pos['opened_ts']})")
    else:
        lines.append("- none")
    a = p["activity"]
    lines += [
        "",
        "## Activity",
        f"- Signals: {a['signals']} · orders: {a['orders']} · fills: {a['fills']} "
        f"· risk-rejected: {max(0, a['signals'] - a['orders'])}",
        f"- News items: {p['news_items']} · admissions: "
        + (", ".join(p["admissions"]) if p["admissions"] else "none"),
    ]
    b = p.get("budgets") or {}
    if b:
        llm = b.get("llm_month_usd")
        si = b.get("stockinsights_today")
        lines.append(
            "- Budgets: LLM month-to-date "
            + (f"${llm:.4f}" if llm is not None else "n/a")
            + " · StockInsights calls today "
            + (str(si) if si is not None else "n/a"))
    lines.append("")
    return "\n".join(lines)
