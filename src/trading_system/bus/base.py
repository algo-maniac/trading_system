"""Bus interfaces — the contract every agent codes against, independent of transport.

`RedisBarBus` implements both protocols today. A future `KafkaBarBus` can implement the
same surface so agents need no change.
"""
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from trading_system.replay.events import BarEvent


@dataclass(frozen=True)
class Alert:
    """An operational event (token expiry, ingest gap, fetch failure) for ops/monitoring."""
    kind: str
    detail: str
    ts: str  # ISO timestamp the alert was raised


class BarBus(Protocol):
    """Transport for finalized OHLCV bars. At-least-once: consumers must `ack` each message."""

    def publish(self, bar: BarEvent) -> str:
        """Append a bar to the stream; returns the transport message id."""
        ...

    def ensure_group(self, group: str, start_id: str = "$") -> None:
        """Idempotently create a consumer group (default: only bars added after creation)."""
        ...

    def poll(
        self, group: str, consumer: str, count: int = 100, block_ms: int = 1000
    ) -> list[tuple[str, BarEvent]]:
        """One read cycle for a consumer group. Returns up to `count` new (msg_id, bar)
        pairs, blocking up to `block_ms` for the first one. Empty list on timeout."""
        ...

    def read_pending(
        self, group: str, consumer: str, count: int = 100
    ) -> list[tuple[str, BarEvent]]:
        """Re-read this consumer's delivered-but-un-acked messages (crash recovery / replay)."""
        ...

    def ack(self, group: str, msg_id: str) -> None:
        """Acknowledge a processed message so it leaves the pending-entries list."""
        ...

    def consume(
        self, group: str, consumer: str, count: int = 100, block_ms: int = 1000
    ) -> Iterator[tuple[str, BarEvent]]:
        """Block forever, yielding (msg_id, bar) as they arrive (live-agent convenience)."""
        ...


class AlertBus(Protocol):
    """Transport for operational alerts."""

    def publish_alert(self, kind: str, detail: str) -> str:
        ...
