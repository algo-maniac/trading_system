"""Redis Streams implementation of BarBus + AlertBus.

Bars go to one stream (`market.bars`) capped with MAXLEN ~; consumers read via consumer
groups (XREADGROUP) and acknowledge with XACK, giving at-least-once delivery with crash
recovery (un-acked messages stay in the group's pending-entries list). Alerts go to a
separate `alerts` stream for ops/monitoring.

Redis is already a project dependency and runs in docker-compose with AOF persistence, so
the bus needs no new infrastructure.
"""
from collections.abc import Iterator
from datetime import UTC, datetime

import redis

from trading_system.bus.serde import bar_to_fields, fields_to_bar
from trading_system.config import get_settings
from trading_system.observability import get_logger
from trading_system.replay.events import BarEvent

logger = get_logger(__name__)


class RedisBarBus:
    """BarBus + AlertBus backed by Redis Streams."""

    def __init__(
        self,
        client: "redis.Redis | None" = None,
        *,
        bar_stream: str | None = None,
        alert_stream: str | None = None,
        maxlen: int | None = None,
    ) -> None:
        s = get_settings()
        # decode_responses=True so stream field maps come back as str->str (matches serde).
        self._r = client or redis.Redis.from_url(s.redis_url, decode_responses=True)
        self._bar_stream = bar_stream or s.bus_bar_stream
        self._alert_stream = alert_stream or s.bus_alert_stream
        self._maxlen = maxlen if maxlen is not None else s.bus_maxlen

    # --- producer ---

    def publish(self, bar: BarEvent) -> str:
        msg_id: str = self._r.xadd(
            self._bar_stream, bar_to_fields(bar), maxlen=self._maxlen, approximate=True
        )
        return msg_id

    def publish_alert(self, kind: str, detail: str) -> str:
        ts = datetime.now(UTC).isoformat()
        msg_id: str = self._r.xadd(self._alert_stream, {"kind": kind, "detail": detail, "ts": ts})
        logger.warning("alert_published", kind=kind, detail=detail)
        return msg_id

    # --- consumer-group plumbing ---

    def ensure_group(self, group: str, start_id: str = "$") -> None:
        try:
            self._r.xgroup_create(self._bar_stream, group, id=start_id, mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):  # group already exists is fine
                raise

    def poll(
        self, group: str, consumer: str, count: int = 100, block_ms: int = 1000
    ) -> list[tuple[str, BarEvent]]:
        self.ensure_group(group)
        resp = self._r.xreadgroup(
            group, consumer, {self._bar_stream: ">"}, count=count, block=block_ms
        )
        return self._flatten(resp)

    def read_pending(
        self, group: str, consumer: str, count: int = 100
    ) -> list[tuple[str, BarEvent]]:
        self.ensure_group(group)
        # id "0" re-delivers this consumer's already-delivered-but-un-acked messages.
        resp = self._r.xreadgroup(group, consumer, {self._bar_stream: "0"}, count=count)
        return self._flatten(resp)

    def ack(self, group: str, msg_id: str) -> None:
        self._r.xack(self._bar_stream, group, msg_id)

    def consume(
        self, group: str, consumer: str, count: int = 100, block_ms: int = 1000
    ) -> Iterator[tuple[str, BarEvent]]:
        """Block forever, yielding new (msg_id, bar) pairs. Caller acks after processing."""
        self.ensure_group(group)
        while True:
            yield from self.poll(group, consumer, count=count, block_ms=block_ms)

    # --- helpers ---

    @staticmethod
    def _flatten(resp: object) -> list[tuple[str, BarEvent]]:
        """XREADGROUP returns [(stream, [(msg_id, fields), ...]), ...] (or None on timeout)."""
        out: list[tuple[str, BarEvent]] = []
        if not resp:
            return out
        for _stream, messages in resp:  # type: ignore[misc]
            for msg_id, fields in messages:
                out.append((msg_id, fields_to_bar(fields)))
        return out
