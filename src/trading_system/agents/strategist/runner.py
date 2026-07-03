"""StrategistRunner: bar stream in → SignalEvents out (no execution, no ledger writes).

Sleeve routing (the Phase 5R/5V validated book):
  - day bars    → `momentum` (xsec_momentum, CROSS-SECTIONAL: batched by timestamp) and
                  `stabilizer` (swing_pullback, per-bar)
  - 5minute bars→ `event_intraday` (gap-ORB, per-bar)

Cross-sectional batching mirrors BacktestEngine._run_batched: day bars are buffered per
timestamp and the sleeve's on_bars() fires when the timestamp advances (or on flush()).
Position awareness comes from read-only LivePortfolio views over the Trader's ledger —
one source of truth for "am I holding X?".

Live day-mode gets exactly ONE batch per calendar day (the EOD reader's single publish
cycle) — waiting for "the timestamp advances" to flush it would mean waiting for
TOMORROW's batch to arrive, delaying every signal by a full trading day. `flush_idle()`
covers this: called periodically from app.py, it flushes any batch that's gone quiet for
longer than a threshold (the reader publishes all ~200 symbols within a few seconds of
each other, so even a short idle window safely distinguishes "still trickling in" from
"today's batch is complete").

Signal ids are deterministic (sleeve|strategy|symbol|ts|side) so the at-least-once bus
can redeliver anything and downstream consumers dedupe. Publishing is an injected
callable → unit tests run without Redis.
"""
import time
from collections.abc import Callable
from dataclasses import dataclass

from trading_system.bus.signals import SignalEvent, signal_id
from trading_system.observability import get_logger
from trading_system.replay.events import BarEvent
from trading_system.strategy.base import PortfolioView, Strategy
from trading_system.strategy.signals import Signal

logger = get_logger(__name__)


@dataclass
class Sleeve:
    name: str
    strategy: Strategy
    interval: str                 # bar interval this sleeve consumes
    view: PortfolioView           # read-only position state (LivePortfolio)


class StrategistRunner:
    def __init__(
        self,
        sleeves: list[Sleeve],
        publish: Callable[[SignalEvent], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sleeves = sleeves
        self._publish = publish
        self._clock = clock
        # per cross-sectional sleeve: (current_ts, buffered bars)
        self._batches: dict[str, tuple[object, list[BarEvent]]] = {}
        # per cross-sectional sleeve: monotonic time of its last bar — drives flush_idle
        self._batch_last_update: dict[str, float] = {}
        # Liveness counters (app.py logs these as a periodic heartbeat — otherwise a
        # sleeve with no fires today is indistinguishable in the logs from a stuck
        # consumer; both are silent).
        self.bars_processed = 0
        self.signals_published = 0

    def on_bar(self, bar: BarEvent) -> int:
        """Route one bar to every matching sleeve. Returns #signals published."""
        self.bars_processed += 1
        published = 0
        for sleeve in self._sleeves:
            if bar.interval != sleeve.interval:
                continue
            if sleeve.strategy.cross_sectional:
                published += self._feed_batched(sleeve, bar)
            else:
                signals = sleeve.strategy.on_bar(bar, sleeve.view)
                published += self._emit(sleeve, bar.ts, {bar.symbol: bar.close}, signals)
        return published

    def flush(self) -> int:
        """Flush any buffered cross-sectional batches (end of session / shutdown)."""
        published = 0
        for sleeve in self._sleeves:
            if sleeve.strategy.cross_sectional:
                published += self._flush_batch(sleeve)
        return published

    def flush_idle(self, idle_seconds: float) -> int:
        """Flush any cross-sectional batch that hasn't received a bar in `idle_seconds`.

        Call periodically from the app loop. Safe to call every iteration — it only
        acts once a batch is genuinely stale, never mid-trickle.
        """
        now = self._clock()
        published = 0
        for sleeve in self._sleeves:
            if not sleeve.strategy.cross_sectional:
                continue
            _, batch = self._batches.get(sleeve.name, (None, []))
            last = self._batch_last_update.get(sleeve.name)
            if batch and last is not None and now - last >= idle_seconds:
                published += self._flush_batch(sleeve)
        return published

    # --- internals -------------------------------------------------------------

    def _feed_batched(self, sleeve: Sleeve, bar: BarEvent) -> int:
        current_ts, batch = self._batches.get(sleeve.name, (None, []))
        published = 0
        if current_ts is not None and bar.ts != current_ts:
            published += self._flush_batch(sleeve)
            current_ts, batch = None, []
        self._batches[sleeve.name] = (bar.ts, batch + [bar])
        self._batch_last_update[sleeve.name] = self._clock()
        return published

    def _flush_batch(self, sleeve: Sleeve) -> int:
        current_ts, batch = self._batches.get(sleeve.name, (None, []))
        if not batch:
            return 0
        self._batches[sleeve.name] = (None, [])
        self._batch_last_update.pop(sleeve.name, None)
        signals = sleeve.strategy.on_bars(batch, sleeve.view)
        prices = {b.symbol: b.close for b in batch}
        return self._emit(sleeve, current_ts, prices, signals)

    def _emit(self, sleeve: Sleeve, ts, prices: dict[str, float], signals: list[Signal]) -> int:
        for sig in signals:
            event = SignalEvent(
                id=signal_id(sleeve.name, sleeve.strategy.name, sig.symbol, ts, sig.side.value),
                ts=ts,
                sleeve=sleeve.name,
                strategy=sleeve.strategy.name,
                symbol=sig.symbol,
                side=sig.side.value,
                size_hint=sig.size_hint,
                stop_pct=sig.stop_pct,
                target_pct=sig.target_pct,
                trailing_stop_pct=sig.trailing_stop_pct,
                trailing_atr_mult=sig.trailing_atr_mult,
                reason=sig.reason,
                ref_price=prices.get(sig.symbol, 0.0),
            )
            self._publish(event)
            self.signals_published += 1
            logger.info(
                "signal_published", sleeve=sleeve.name, symbol=sig.symbol,
                side=sig.side.value, reason=sig.reason, sid=event.id[:12],
            )
        return len(signals)
