"""LLM clients behind a swappable protocol.

`llm_provider` in settings picks the implementation: "mock" (deterministic heuristics,
free — the replay default) or "anthropic" (real Claude Haiku reviews, budget-guarded).
Everything downstream talks to the LLMClient protocol only.
"""
from trading_system.config import get_settings
from trading_system.llm.base import LLMClient


def get_llm() -> LLMClient:
    provider = get_settings().llm_provider
    if provider == "mock":
        from trading_system.llm.mock import MockLLM
        return MockLLM()
    if provider == "anthropic":
        from trading_system.llm.anthropic_client import AnthropicLLM
        return AnthropicLLM()
    raise ValueError(f"unknown llm_provider '{provider}' (expected 'mock' or 'anthropic')")
