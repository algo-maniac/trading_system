"""MockLLM: deterministic admission heuristics standing in for a real model.

Leans on StockInsights' own AI tags (sentiment + announcement type) plus the quant
gates the pipeline already computed — a defensible rule-based reviewer, not a fake.
Deterministic on purpose: unit tests pin its behavior, and when the real Anthropic
client replaces it (config flip), the shadow-comparison between the two is meaningful.
"""
from trading_system.llm.base import AdmissionContext, Verdict

# Announcement types that plausibly justify adding a NEW name to the tradable universe.
# Conservative by design — routine filings (AGM notices, trading windows, compliance
# certificates) are never admission-worthy even when tagged positive.
_MATERIAL_TYPES = {
    "earnings", "financial results", "order win", "new order", "contract award",
    "acquisition", "merger", "demerger", "expansion", "capacity expansion",
    "fund raise", "fund raising", "preferential issue", "buyback", "bonus issue",
    "stock split", "credit rating", "rating upgrade", "joint venture", "stake sale",
    "new product", "regulatory approval", "license",
}


class MockLLM:
    name = "mock"

    def review_admission(self, context: AdmissionContext) -> Verdict:
        if not context.quant.get("passed_gates", False):
            return Verdict(False, 0.95, "quant gates failed — not liquid/seasoned enough "
                                        "regardless of the news")
        if context.sentiment == "negative":
            return Verdict(False, 0.8, f"negative-sentiment news ({context.announcement_type}) "
                                       "is not grounds to ADD a name to the long-only universe")
        type_lower = (context.announcement_type or "").lower()
        material = any(t in type_lower for t in _MATERIAL_TYPES)
        if not material:
            return Verdict(False, 0.7, f"announcement type '{context.announcement_type}' is "
                                       "routine — not admission-worthy")
        return Verdict(True, 0.6, f"material {context.sentiment} news "
                                  f"('{context.announcement_type}') on a name that passed "
                                  "liquidity/history gates")
