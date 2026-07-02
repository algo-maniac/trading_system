"""Forward / paper-test driver: feed live bars off the bus into a persistent engine.

The same `BacktestEngine` that runs historic batch backtests is driven incrementally via
`engine.feed(bar)` as bars arrive on the bus. No real orders are placed — fills, fees and
slippage are simulated exactly as in a backtest, so the forward run is a true paper-trade.

Equivalence guarantee: feeding a recorded, (ts, symbol)-ordered bar sequence through here
yields the same trades/metrics as `BacktestEngine.run()` over the same bars (the engine shares
one `feed`/`finalize` path) — this is what makes "live test" trustworthy. See
tests/test_forward.py.

Delivery is at-least-once: each polled message is acked after it is fed. A crash between
feed and ack would re-deliver that bar on restart (rare double-process); acceptable for paper
trading, and the operator sees it in the logs.
"""
from collections.abc import Callable

from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.results import BacktestResult
from trading_system.bus.base import BarBus
from trading_system.observability import get_logger
from trading_system.replay.events import BarEvent

logger = get_logger(__name__)


class ForwardTester:
    """Consume bars from the bus for one strategy/interval and push them through the engine."""

    def __init__(
        self,
        engine: BacktestEngine,
        bus: BarBus,
        *,
        group: str,
        consumer: str,
        interval: str,
        symbols: list[str] | None = None,
        start_id: str = "$",
    ) -> None:
        self.engine = engine
        self.bus = bus
        self.group = group
        self.consumer = consumer
        self.interval = interval
        self.symbols = set(symbols) if symbols else None
        self.fed = 0
        # Create the group at the desired start position ("$" = live-only, "0" = replay history).
        self.bus.ensure_group(group, start_id=start_id)

    def _accept(self, bar: BarEvent) -> bool:
        if bar.interval != self.interval:
            return False
        return not (self.symbols and bar.symbol not in self.symbols)

    def step(self, count: int = 100, block_ms: int = 1000) -> int:
        """One poll cycle: feed accepted bars into the engine, ack every polled message.
        Returns the number of bars fed this cycle."""
        fed_this_cycle = 0
        for msg_id, bar in self.bus.poll(self.group, self.consumer, count=count, block_ms=block_ms):
            if self._accept(bar):
                self.engine.feed(bar)
                fed_this_cycle += 1
            self.bus.ack(self.group, msg_id)
        self.fed += fed_this_cycle
        if fed_this_cycle:
            logger.info(
                "forward_fed",
                bars=fed_this_cycle,
                total=self.fed,
                equity=round(self.engine.portfolio.equity(), 2),
                open_positions=len(self.engine.portfolio.positions),
            )
        return fed_this_cycle

    def run_until(self, should_stop: Callable[[], bool], block_ms: int = 1000) -> None:
        """Poll-and-feed loop until `should_stop()` returns True (e.g. session close / signal)."""
        while not should_stop():
            self.step(block_ms=block_ms)

    def result(self) -> BacktestResult:
        """Finalize the engine (one trailing snapshot) and return the paper-trade result."""
        return self.engine.finalize()
