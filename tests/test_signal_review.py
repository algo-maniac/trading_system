"""Signal reviewer agent: tool-use loop, fail-OPEN on every failure path, point-in-time
tool payloads, the reviewer core's dedupe/side filter, and the eval harness metrics."""
from datetime import UTC, datetime
from types import SimpleNamespace

from trading_system.agents.reviewer.core import ReviewerCore, ReviewerDeps
from trading_system.agents.reviewer.repo import summarize_closes
from trading_system.analytics.review_eval import ReviewOutcome, evaluate
from trading_system.bus.signals import SignalEvent
from trading_system.llm.signal_review import (
    TOOL_DEFS,
    AnthropicSignalReviewer,
    MockSignalReviewer,
    SignalVerdict,
)
from trading_system.net.limits import BudgetExhausted

TS = datetime(2025, 3, 3, 10, 0, tzinfo=UTC)


def _sig(side="BUY", symbol="ACME", sid="s1"):
    return SignalEvent(id=sid, ts=TS, sleeve="stabilizer", strategy="swing_pullback",
                       symbol=symbol, side=side, ref_price=100.0, reason="rsi<35",
                       features={"rsi_14": 31.2})


class FakeTools:
    def __init__(self, positions=(), news=(), ret_20=0.05):
        self._positions, self._news, self._ret = list(positions), list(news), ret_20
        self.calls: list[str] = []

    def position(self, symbol):
        self.calls.append("position")
        return {"symbol": symbol, "positions": self._positions}

    def recent_news(self, symbol, days):
        self.calls.append(f"news:{days}")
        return {"symbol": symbol, "items": self._news}

    def price_history(self, symbol, bars):
        self.calls.append(f"prices:{bars}")
        return {"symbol": symbol, "return_20d": self._ret}

    def sleeve_exposure(self, sleeve):
        self.calls.append("exposure")
        return {"sleeve": sleeve, "cash": 50_000.0, "open_positions": 2, "symbols": []}


class FakeBudget:
    def __init__(self, exhausted=False):
        self.exhausted = exhausted

    def try_spend(self, n=1):
        if self.exhausted:
            raise BudgetExhausted("signal_review", 20)


class FakeSpend:
    def __init__(self, over_cap=False):
        self.over_cap = over_cap
        self.added: list[float] = []

    def precheck(self):
        if self.over_cap:
            raise BudgetExhausted("llm", 2)

    def add_usd(self, cost):
        self.added.append(cost)
        return sum(self.added)

    def crossed_alert_threshold(self, total):
        return False


def _tool_use(name, args, uid):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=uid)


def _resp(*blocks, stop="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


class ScriptedClient:
    """Returns canned responses in order and records every request's messages."""

    def __init__(self, *responses, raise_exc=None):
        self._responses = list(responses)
        self._raise = raise_exc
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        if self._raise:
            raise self._raise
        return self._responses.pop(0)


def _reviewer(client, budget=None, spend=None, alerts=None, max_turns=6):
    sink = alerts if alerts is not None else []
    return AnthropicSignalReviewer(
        client=client, call_budget=budget or FakeBudget(), spend=spend or FakeSpend(),
        alert=lambda kind, msg: sink.append(kind), model="claude-sonnet-5",
        max_turns=max_turns)


# --- the agent loop -------------------------------------------------------------------

def test_tools_are_strict_and_include_submit_verdict():
    names = {t["name"] for t in TOOL_DEFS}
    assert {"get_position", "get_recent_news", "get_price_history",
            "get_sleeve_exposure", "submit_verdict"} == names
    for t in TOOL_DEFS:
        assert t["strict"] is True
        assert t["input_schema"]["additionalProperties"] is False


def test_agent_gathers_evidence_then_submits_verdict():
    client = ScriptedClient(
        _resp(_tool_use("get_position", {"symbol": "ACME"}, "t1"),
              _tool_use("get_recent_news", {"symbol": "ACME", "days": 99}, "t2")),
        _resp(_tool_use("submit_verdict", {"approve": False, "confidence": 0.7,
                                           "reasoning": "negative filing yesterday"}, "t3")),
    )
    spend = FakeSpend()
    tools = FakeTools(news=[{"sentiment": "negative"}])
    v = _reviewer(client, spend=spend).review(_sig(), tools)

    assert (v.approve, v.confidence, v.error) == (False, 0.7, None)
    assert v.tools_used == ["get_position", "get_recent_news"]
    assert tools.calls == ["position", "news:30"]           # days clamped to 30
    # second request carries assistant tool_use turn + one user turn with BOTH results
    second = client.requests[1]["messages"]
    assert second[1]["role"] == "assistant"
    results = second[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert all(r["is_error"] is False for r in results)
    assert len(spend.added) == 2 and v.cost_usd > 0          # each turn costed
    assert "temperature" not in client.requests[0]


def test_bad_tool_call_returns_is_error_and_loop_continues():
    client = ScriptedClient(
        _resp(_tool_use("get_balance_sheet", {"symbol": "ACME"}, "t1")),
        _resp(_tool_use("submit_verdict", {"approve": True, "confidence": 0.5,
                                           "reasoning": "ok"}, "t2")),
    )
    v = _reviewer(client).review(_sig(), FakeTools())
    assert v.approve is True and v.error is None
    assert client.requests[1]["messages"][2]["content"][0]["is_error"] is True


def test_text_only_turn_is_nudged_toward_submit_verdict():
    client = ScriptedClient(
        _resp(SimpleNamespace(type="text", text="Looks fine."), stop="end_turn"),
        _resp(_tool_use("submit_verdict", {"approve": True, "confidence": 0.6,
                                           "reasoning": "fine"}, "t1")),
    )
    v = _reviewer(client).review(_sig(), FakeTools())
    assert v.approve is True
    assert "submit_verdict" in client.requests[1]["messages"][-1]["content"]


def test_fails_open_on_api_error():
    v = _reviewer(ScriptedClient(raise_exc=RuntimeError("503"))).review(_sig(), FakeTools())
    assert v.approve is True and v.error.startswith("llm_error")


def test_fails_open_on_daily_budget_without_calling_api():
    client, alerts = ScriptedClient(), []
    v = _reviewer(client, budget=FakeBudget(exhausted=True), alerts=alerts).review(
        _sig(), FakeTools())
    assert v.approve is True and "budget_exhausted" in v.error
    assert client.requests == [] and alerts == ["signal_review_budget_exhausted"]


def test_fails_open_on_spend_cap():
    client = ScriptedClient()
    v = _reviewer(client, spend=FakeSpend(over_cap=True)).review(_sig(), FakeTools())
    assert v.approve is True and v.error.startswith("spend_cap") and client.requests == []


def test_fails_open_when_turns_run_out():
    loop = [_resp(_tool_use("get_position", {"symbol": "ACME"}, f"t{i}")) for i in range(3)]
    v = _reviewer(ScriptedClient(*loop), max_turns=3).review(_sig(), FakeTools())
    assert v.approve is True and v.error == "max_turns_without_verdict"
    assert v.tools_used == ["get_position"] * 3


def test_fails_open_on_out_of_range_verdict_and_refusal():
    bad = ScriptedClient(_resp(_tool_use("submit_verdict", {"approve": False,
                                                            "confidence": 3.0,
                                                            "reasoning": "x"}, "t1")))
    assert _reviewer(bad).review(_sig(), FakeTools()).error.startswith("llm_error")
    refused = ScriptedClient(_resp(stop="refusal"))
    assert _reviewer(refused).review(_sig(), FakeTools()).error == "model_refusal"


# --- mock reviewer (rules baseline) ----------------------------------------------------

def test_mock_reviewer_vetoes_on_evidence():
    m = MockSignalReviewer()
    assert m.review(_sig(), FakeTools(positions=[{"sleeve": "momentum"}])).approve is False
    assert m.review(_sig(), FakeTools(news=[{"sentiment": "negative"}])).approve is False
    assert m.review(_sig(), FakeTools(ret_20=0.40)).approve is False
    ok = m.review(_sig(), FakeTools())
    assert ok.approve is True
    assert ok.tools_used == ["get_position", "get_recent_news", "get_price_history"]


# --- reviewer core ---------------------------------------------------------------------

def test_core_reviews_buys_once_and_skips_exits():
    saved: dict[str, SignalVerdict] = {}
    core = ReviewerCore(ReviewerDeps(
        review=MockSignalReviewer().review,
        tools_for=lambda sig: FakeTools(),
        already_reviewed=lambda sid: sid in saved,
        save=lambda sig, v: saved.setdefault(sig.id, v) is v,
    ))
    assert core.on_signal(_sig()) is not None
    assert core.on_signal(_sig()) is None                     # redelivery → deduped
    assert core.on_signal(_sig(side="SELL", sid="s2")) is None  # exits never reviewed
    assert list(saved) == ["s1"] and (core.reviewed, core.skipped) == (1, 2)


# --- tool payloads + eval harness ------------------------------------------------------

def test_summarize_closes_stats():
    bars = [(f"2025-01-{i + 1:02d}", 100.0 + i, 1000) for i in range(21)]
    out = summarize_closes("ACME", bars)
    assert out["return_20d"] == round(120.0 / 100.0 - 1.0, 4)
    assert out["pct_below_high"] == 0.0 and out["volatility_20d"] is not None
    short = summarize_closes("ACME", bars[:5])
    assert short["return_20d"] is None and len(short["bars"]) == 5


def test_evaluate_scores_vetoes_as_loser_detection():
    rows = [
        ReviewOutcome("a", True, 0.05),    # approved winner
        ReviewOutcome("b", True, -0.02),   # approved loser (missed)
        ReviewOutcome("c", False, -0.04),  # vetoed loser (caught)
        ReviewOutcome("d", False, 0.03),   # vetoed winner (false alarm)
    ]
    rep = evaluate(rows, cost=0.002)
    assert (rep.n, rep.approved, rep.vetoed, rep.losers) == (4, 2, 2, 2)
    assert rep.precision == 0.5 and rep.recall == 0.5 and rep.accuracy == 0.5
    assert abs(rep.mean_return_all - 0.005) < 1e-12
    assert abs(rep.lift - (0.015 - 0.005)) < 1e-12


def test_evaluate_handles_no_vetoes():
    rep = evaluate([ReviewOutcome("a", True, 0.01)])
    assert rep.precision is None and rep.recall is None and rep.lift == 0.0
