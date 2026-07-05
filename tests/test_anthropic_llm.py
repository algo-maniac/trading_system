"""AnthropicLLM: JSON verdict parsing, fail-CLOSED on every failure path, budget gates
before any API call, spend accounting + one-time alert — all with a fake SDK client."""
from unittest.mock import MagicMock

import pytest

from trading_system.llm.anthropic_client import AnthropicLLM, _parse_verdict
from trading_system.llm.base import AdmissionContext
from trading_system.net.limits import BudgetExhausted


def _context(**over):
    base = dict(
        symbol="NEWCO", company_name="Newco Ltd", announcement_type="Order Win",
        sentiment="positive", summary_header="hdr", summary_text="txt",
        quant={"median_turnover_cr": 80.0, "sessions": 300, "avg_range_pct": 0.03,
               "passed_gates": True},
    )
    base.update(over)
    return AdmissionContext(**base)


class FakeCallBudget:
    def __init__(self, exhausted=False):
        self.exhausted = exhausted
        self.spent = 0

    def try_spend(self, n=1):
        if self.exhausted:
            raise BudgetExhausted("llm", 100)
        self.spent += n


class FakeSpend:
    def __init__(self, over_cap=False, alert_on=None):
        self.over_cap = over_cap
        self.alert_on = alert_on          # total at which crossed_alert returns True once
        self.added: list[float] = []
        self._alerted = False

    def precheck(self):
        if self.over_cap:
            raise BudgetExhausted("llm", 2)

    def add_usd(self, cost):
        self.added.append(cost)
        return sum(self.added)

    def crossed_alert_threshold(self, total):
        if self.alert_on is not None and total >= self.alert_on and not self._alerted:
            self._alerted = True
            return True
        return False


def _response(text, input_tokens=1000, output_tokens=200):
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    r.usage.input_tokens = input_tokens
    r.usage.output_tokens = output_tokens
    return r


def _llm(response=None, side_effect=None, call_budget=None, spend=None, alerts=None):
    client = MagicMock()
    if side_effect is not None:
        client.messages.create.side_effect = side_effect
    else:
        client.messages.create.return_value = response
    alerts = alerts if alerts is not None else []
    llm = AnthropicLLM(client=client, call_budget=call_budget or FakeCallBudget(),
                       spend=spend or FakeSpend(),
                       alert=lambda kind, detail: alerts.append(kind))
    return llm, client, alerts


def test_happy_path_parses_verdict_and_records_spend():
    spend = FakeSpend()
    llm, client, _ = _llm(_response(
        '{"admit": true, "confidence": 0.8, "reasoning": "material order win"}'), spend=spend)
    v = llm.review_admission(_context())
    assert v.admit is True and v.confidence == 0.8
    assert client.messages.create.call_count == 1
    # 1000 in @ $1/M + 200 out @ $5/M = $0.001 + $0.001 = $0.002
    assert spend.added == [pytest.approx(0.002)]


def test_fenced_json_still_parses():
    llm, _, _ = _llm(_response(
        '```json\n{"admit": false, "confidence": 0.9, "reasoning": "routine"}\n```'))
    v = llm.review_admission(_context())
    assert v.admit is False and v.confidence == 0.9


def test_malformed_json_fails_closed():
    llm, _, _ = _llm(_response("I think we should admit this stock!"))
    v = llm.review_admission(_context())
    assert v.admit is False and "llm_error" in v.reasoning


def test_sdk_exception_fails_closed():
    llm, _, _ = _llm(side_effect=RuntimeError("connection reset"))
    v = llm.review_admission(_context())
    assert v.admit is False and "llm_error" in v.reasoning


def test_call_budget_exhausted_fails_closed_without_api_call():
    llm, client, alerts = _llm(_response("{}"), call_budget=FakeCallBudget(exhausted=True))
    v = llm.review_admission(_context())
    assert v.admit is False and "llm_budget_exhausted" in v.reasoning
    assert client.messages.create.call_count == 0
    assert alerts == ["llm_budget_exhausted"]


def test_monthly_cap_reached_fails_closed_without_api_call():
    llm, client, alerts = _llm(_response("{}"), spend=FakeSpend(over_cap=True))
    v = llm.review_admission(_context())
    assert v.admit is False and "llm_budget_exhausted" in v.reasoning
    assert client.messages.create.call_count == 0
    assert alerts == ["llm_budget_exhausted"]


def test_spend_alert_fires_once():
    spend = FakeSpend(alert_on=0.001)
    ok = '{"admit": true, "confidence": 0.5, "reasoning": "r"}'
    alerts: list[str] = []
    llm, _, _ = _llm(_response(ok), spend=spend, alerts=alerts)
    llm.review_admission(_context())
    llm.review_admission(_context())
    assert alerts == ["llm_spend_alert"]


def test_parse_verdict_rejects_out_of_range_confidence():
    with pytest.raises(ValueError):
        _parse_verdict('{"admit": true, "confidence": 3.0, "reasoning": "r"}')


def test_parse_verdict_rejects_non_bool_admit():
    with pytest.raises(ValueError):
        _parse_verdict('{"admit": "yes", "confidence": 0.5, "reasoning": "r"}')
