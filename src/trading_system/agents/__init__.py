"""Agents — long-running service processes that compose the shared trading_system core.

Each agent owns one responsibility and communicates over the bus:
- reader:     ingests candles (scheduled + on-demand) and publishes bars.
- backtester: backtests historic data and forward/paper-tests live bars off the bus.

New agents (execution, news/LLM, monitoring) attach to the same bus over time.
"""
