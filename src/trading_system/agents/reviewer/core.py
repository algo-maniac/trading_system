"""ReviewerCore: signal → (dedupe) → reviewer agent → journal. All effects injected."""
from collections.abc import Callable
from dataclasses import dataclass

from trading_system.bus.signals import SignalEvent
from trading_system.llm.signal_review import ReviewTools, SignalVerdict
from trading_system.observability import get_logger

logger = get_logger(__name__)


@dataclass
class ReviewerDeps:
    review: Callable[[SignalEvent, ReviewTools], SignalVerdict]
    tools_for: Callable[[SignalEvent], ReviewTools]       # point-in-time tools per signal
    already_reviewed: Callable[[str], bool]
    save: Callable[[SignalEvent, SignalVerdict], bool]


class ReviewerCore:
    def __init__(self, deps: ReviewerDeps) -> None:
        self._d = deps
        self.reviewed = 0
        self.skipped = 0

    def on_signal(self, sig: SignalEvent) -> SignalVerdict | None:
        # Exits are protection (stops, square-off) — never second-guessed.
        if sig.side != "BUY" or self._d.already_reviewed(sig.id):
            self.skipped += 1
            return None
        verdict = self._d.review(sig, self._d.tools_for(sig))
        self._d.save(sig, verdict)
        self.reviewed += 1
        return verdict
