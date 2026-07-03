"""Analyst agent: the daily market-analysis brain (Phase 5c).

Sweeps StockInsights news → journal + gap watchlist; runs the admission pipeline
for news-discovered names (backfill → quant gates → LLM review → universe_members);
builds the day's focus_list. It never trades and never publishes bars — it decides
WHAT the readers should cover today.
"""
