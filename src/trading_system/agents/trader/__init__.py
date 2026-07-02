"""Trader agent: the only component that executes. Consumes SignalEvents, gates them
through RiskManager, places orders via the Broker protocol, owns the persistent ledger,
and protects open positions (stops/targets/trailing/MIS square-off) engine-style."""
