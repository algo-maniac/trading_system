"""Eval harness for the shadow signal reviewer: did its vetoes avoid losing trades?

Each reviewed BUY is labelled by what actually happened next: the forward return from
the signal's reference price to the close `horizon` daily bars later. A trade is a
"loser" if that return doesn't clear `cost` (round-trip fees + slippage). The reviewer
is then scored as a loser-detector:

  precision  — of the signals it vetoed, how many were losers   (vetoes were justified)
  recall     — of the losers, how many it vetoed                 (losers it caught)
  lift       — mean forward return of approved signals minus the mean of all signals
               (positive ⇒ following the reviewer would have improved the book)

Fail-open rows (error set) are excluded: an unavailable reviewer made no decision.
The pure `evaluate` takes labelled rows so it is testable without a database.
"""
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class ReviewOutcome:
    signal_id: str
    approve: bool
    forward_return: float


@dataclass(frozen=True)
class EvalReport:
    n: int
    approved: int
    vetoed: int
    losers: int
    precision: float | None      # losers among vetoes
    recall: float | None         # vetoes among losers
    accuracy: float | None       # approve-winner + veto-loser over all
    mean_return_all: float | None
    mean_return_approved: float | None
    mean_return_vetoed: float | None
    lift: float | None


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def evaluate(rows: list[ReviewOutcome], cost: float = 0.002) -> EvalReport:
    losers = [r for r in rows if r.forward_return <= cost]
    vetoed = [r for r in rows if not r.approve]
    approved = [r for r in rows if r.approve]
    vetoed_losers = sum(1 for r in vetoed if r.forward_return <= cost)
    approved_winners = sum(1 for r in approved if r.forward_return > cost)
    mean_all = _mean([r.forward_return for r in rows])
    mean_app = _mean([r.forward_return for r in approved])
    return EvalReport(
        n=len(rows), approved=len(approved), vetoed=len(vetoed), losers=len(losers),
        precision=_ratio(vetoed_losers, len(vetoed)),
        recall=_ratio(vetoed_losers, len(losers)),
        accuracy=_ratio(vetoed_losers + approved_winners, len(rows)),
        mean_return_all=mean_all,
        mean_return_approved=mean_app,
        mean_return_vetoed=_mean([r.forward_return for r in vetoed]),
        lift=(mean_app - mean_all) if mean_app is not None and mean_all is not None else None,
    )


_OUTCOMES_SQL = """
    SELECT r.signal_id, r.approve, r.ref_price, fwd.close
    FROM signal_reviews r
    JOIN instruments i ON i.tradingsymbol = r.symbol AND i.exchange = 'NSE'
    CROSS JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_token = i.instrument_token AND o.interval = 'day' AND o.ts > r.ts
        ORDER BY o.ts OFFSET %(offset)s LIMIT 1
    ) fwd
    WHERE r.error IS NULL AND r.side = 'BUY' AND r.ref_price > 0
      AND (%(reviewer)s::text IS NULL OR r.reviewer = %(reviewer)s::text)
    ORDER BY r.ts
"""


def load_outcomes(conn: psycopg.Connection, horizon: int = 5,  # type: ignore[type-arg]
                  reviewer: str | None = None) -> list[ReviewOutcome]:
    """Label each journaled verdict with its forward return. Signals too recent to have
    `horizon` later bars are simply absent (LATERAL finds no row)."""
    with conn.cursor() as cur:
        cur.execute(_OUTCOMES_SQL, {"offset": horizon - 1, "reviewer": reviewer})
        return [ReviewOutcome(r[0], bool(r[1]), float(r[3]) / float(r[2]) - 1.0)
                for r in cur.fetchall()]
