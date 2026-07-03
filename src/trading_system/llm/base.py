"""LLMClient protocol + shared decision types.

The interface is the contract that matters now: the Analyst's admission pipeline calls
`review_admission` with everything it knows (the news item, quant stats, price profile)
and gets a structured verdict back. MockLLM implements it with deterministic rules so
the pipeline is buildable/testable today; AnthropicLLM slots in later by config flip.
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class AdmissionContext:
    """Everything the reviewer sees about a news-discovered candidate symbol."""
    symbol: str
    company_name: str
    announcement_type: str
    sentiment: str                 # StockInsights tag: positive | negative | neutral
    summary_header: str
    summary_text: str
    quant: dict = field(default_factory=dict)   # type: ignore[type-arg]
    # quant keys (from the admission pipeline's gates):
    #   median_turnover_cr, sessions, avg_range_pct, passed_gates: bool


@dataclass(frozen=True)
class Verdict:
    admit: bool
    confidence: float              # 0..1
    reasoning: str


class LLMClient(Protocol):
    name: str

    def review_admission(self, context: AdmissionContext) -> Verdict:
        """Should this news-discovered symbol be admitted to the tradable universe?"""
        ...
