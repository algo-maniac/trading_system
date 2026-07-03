"""AnthropicLLM: the real reviewer — stubbed until the Claude API plan is purchased.

When activated (settings.llm_provider = "anthropic" + ANTHROPIC_API_KEY in .env):
  - claude-haiku-4-5 for admission reviews (cheap, high volume)
  - budget-guarded via net.limits.DailyBudget("llm", settings.llm_daily_call_budget)
  - failures/timeouts fail CLOSED for admissions (don't add unvetted names) — note this
    is the opposite of the Phase 5d signal-gate, which fails OPEN (rules are validated).
"""
from trading_system.llm.base import AdmissionContext, Verdict


class AnthropicLLM:
    name = "anthropic"

    def review_admission(self, context: AdmissionContext) -> Verdict:
        raise NotImplementedError(
            "AnthropicLLM is not wired yet — purchase the Claude API plan, set "
            "ANTHROPIC_API_KEY in .env and llm_provider=anthropic in settings."
        )
