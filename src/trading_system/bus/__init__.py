"""Message bus between service agents.

The Reader agent publishes finalized bars; the Backtester agent consumes them for
forward/paper-testing. The bus is hidden behind the `BarBus`/`AlertBus` protocols in
`base.py` so the Redis Streams implementation (`redis_streams.py`) can be swapped for a
Kafka one later without touching the agents.
"""
from trading_system.bus.base import Alert, AlertBus, BarBus
from trading_system.bus.redis_streams import RedisBarBus
from trading_system.bus.serde import bar_to_fields, fields_to_bar

__all__ = [
    "Alert",
    "AlertBus",
    "BarBus",
    "RedisBarBus",
    "bar_to_fields",
    "fields_to_bar",
]
