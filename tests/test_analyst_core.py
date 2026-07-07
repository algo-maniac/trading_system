"""AnalystCore: sweep, admission gates (quant + LLM + cap), focus-list priority/cap,
budget-exhaustion degradation — all DB/network-free via injected fakes."""
from datetime import date, datetime

from trading_system.agents.analyst.core import AnalystCore, AnalystDeps
from trading_system.data.stockinsights_client import NewsItem, _hash_link
from trading_system.llm.mock import MockLLM
from trading_system.net.limits import BudgetExhausted

TODAY = date(2026, 7, 3)
NOW = datetime(2026, 7, 3, 8, 15)


def _item(sym, sentiment="positive", ann_type="Order Win", link=None, significant=False):
    link = link or f"https://nse.example/{sym}"
    return NewsItem(
        source_link=link, source_link_hash=_hash_link(link), nse_symbol=sym,
        company_name=f"{sym} Ltd", announcement_type_id=5, announcement_type=ann_type,
        sentiment=sentiment, significant=significant, summary_header="hdr",
        summary_text="txt", published_ts=None, raw={},
    )


class Recorder:
    def __init__(self, *, sessions=300, turnover_cr=80.0, known=True, admissions_today=0):
        self.stored, self.gap, self.backfilled, self.admitted, self.alerts = [], [], [], [], []
        self.focus_written = {}
        self.rejected: set = set()
        self._sessions = sessions
        self._turnover = turnover_cr
        self._known = known
        self._admissions_today = admissions_today

    def deps(self, **over):
        d = AnalystDeps(
            fetch_news=lambda f, t: [],
            store_news=lambda items: len(self.stored) or self.stored.extend(items) or len(items),
            news_id_lookup=lambda h: 1,
            upsert_gap_watch=lambda d_, s, dir_, nid: self.gap.append((s, dir_)),
            known_nse_symbols=lambda syms: set(syms) if self._known else set(),
            backfill_history=lambda syms: self.backfilled.extend(syms),
            quant_stats=lambda s: {"median_turnover": self._turnover * 1e7,
                                   "sessions": self._sessions, "avg_range_pct": 0.03},
            total_sessions=lambda s: self._sessions,
            llm=MockLLM(),
            admit_member=lambda s, prov: self.admitted.append((s, prov)) or True,
            active_members=lambda: [],
            volatile_screen=lambda: [],
            liquid_symbols=lambda: [],
            held_symbols=lambda: [],
            write_focus=lambda d_, entries: self.focus_written.update(entries) or len(entries),
            alert=lambda kind, detail: self.alerts.append(kind),
            admissions_today_count=lambda d: self._admissions_today,
            rejected_today=lambda d: set(self.rejected),
            mark_rejected=lambda s, d: self.rejected.add(s),
        )
        for k, v in over.items():
            setattr(d, k, v)
        return d


class CountingLLM:
    """Wraps MockLLM's verdicts while counting how many review calls actually happened —
    used to prove the quant-gate short-circuit skips the round-trip entirely."""
    name = "counting-mock"

    def __init__(self):
        self.calls = 0
        self._inner = MockLLM()

    def review_admission(self, context):
        self.calls += 1
        return self._inner.review_admission(context)


def test_sweep_stores_and_builds_gap_watch():
    rec = Recorder()
    deps = rec.deps(fetch_news=lambda f, t: [_item("ABC"), _item("XYZ", sentiment="negative",
                                                                 ann_type="Rating Downgrade")])
    core = AnalystCore(deps)
    result = core.run_sweep(NOW)
    assert result["status"] == "ok" and result["fetched"] == 2
    assert ("ABC", "up") in rec.gap and ("XYZ", "down") in rec.gap


def test_sweep_budget_exhaustion_degrades_with_alert_not_crash():
    rec = Recorder()

    def boom(f, t):
        raise BudgetExhausted("stockinsights", 100)

    core = AnalystCore(rec.deps(fetch_news=boom))
    result = core.run_sweep(NOW)
    assert result["status"] == "budget_exhausted"
    assert rec.alerts == ["news_budget_exhausted"]


def test_admission_happy_path_admits_with_provenance():
    rec = Recorder()
    core = AnalystCore(rec.deps())
    admitted = core.consider_admissions([_item("NEWCO")], TODAY)
    assert admitted == ["NEWCO"]
    sym, prov = rec.admitted[0]
    assert prov["quant"]["passed_gates"] is True
    assert prov["verdict"]["reviewer"] == "mock" and prov["verdict"]["admit"] is True


def test_admission_rejects_illiquid_name():
    rec = Recorder(turnover_cr=5.0)   # below the ₹50cr gate
    core = AnalystCore(rec.deps())
    assert core.consider_admissions([_item("THINCO")], TODAY) == []
    assert rec.admitted == []


def test_admission_rejects_short_history_and_backfills_first():
    rec = Recorder(sessions=100)      # below 260-session gate even after backfill
    core = AnalystCore(rec.deps())
    assert core.consider_admissions([_item("IPOCO")], TODAY) == []
    assert rec.backfilled == ["IPOCO"]   # it DID try to fetch history before judging


def test_admission_rejects_negative_news_and_routine_types():
    rec = Recorder()
    core = AnalystCore(rec.deps())
    assert core.consider_admissions([_item("BADCO", sentiment="negative")], TODAY) == []
    assert core.consider_admissions(
        [_item("DULLCO", ann_type="Trading Window Closure")], TODAY) == []


def test_admission_respects_daily_cap():
    rec = Recorder()
    deps = rec.deps()
    deps.admission_cap_per_day = 2
    core = AnalystCore(deps)
    items = [_item(f"CO{i}", link=f"https://x/{i}") for i in range(5)]
    admitted = core.consider_admissions(items, TODAY)
    assert len(admitted) == 2


def test_admission_skips_unknown_and_already_tradable_symbols():
    rec = Recorder(known=False)       # instruments table doesn't know these names
    core = AnalystCore(rec.deps())
    assert core.consider_admissions([_item("GHOST")], TODAY) == []
    rec2 = Recorder()
    core2 = AnalystCore(rec2.deps(liquid_symbols=lambda: ["RELIANCE"]))
    assert core2.consider_admissions([_item("RELIANCE")], TODAY) == []


def test_focus_priority_and_cap_never_evicts_held():
    rec = Recorder()
    deps = rec.deps(
        held_symbols=lambda: ["HELD1", "HELD2"],
        liquid_symbols=lambda: [f"LIQ{i}" for i in range(13)],
        volatile_screen=lambda: [f"VOL{i}" for i in range(60)],   # overflows the cap
    )
    core = AnalystCore(deps)
    entries = core.build_focus(TODAY, news_symbols=["NEWS1"], gap_symbols=["GAP1"])
    assert len(entries) == 50
    assert "HELD1" in entries and "HELD2" in entries          # held survive the cap
    assert "NEWS1" in entries and "GAP1" in entries           # higher priority than screen
    assert "VOL59" not in entries                             # screen overflow evicted


def test_focus_merges_sources_for_same_symbol():
    rec = Recorder()
    deps = rec.deps(held_symbols=lambda: ["DUAL"], liquid_symbols=lambda: ["DUAL"])
    core = AnalystCore(deps)
    entries = core.build_focus(TODAY, news_symbols=[], gap_symbols=[])
    assert entries["DUAL"] == ["held", "liquid"]


def test_admission_skips_names_already_in_base_universe():
    """Caught live: NESTLEIND (a Nifty-200 name) was 'admitted' because the base
    swing universe wasn't in the exclusion set."""
    rec = Recorder()
    core = AnalystCore(rec.deps(base_universe=lambda: ["NESTLEIND"]))
    assert core.consider_admissions([_item("NESTLEIND")], TODAY) == []
    assert rec.admitted == []


def test_admission_cap_is_derived_not_in_process_state():
    """The cap must come from admissions_today_count (DB-backed), not an instance
    attribute — so a restarted/rebuilt AnalystCore can't silently reset it to zero."""
    rec = Recorder(admissions_today=8)
    deps = rec.deps()
    deps.admission_cap_per_day = 10
    core = AnalystCore(deps)
    items = [_item(f"CO{i}", link=f"https://x/{i}") for i in range(5)]
    admitted = core.consider_admissions(items, TODAY)
    assert len(admitted) == 2   # only 2 of the day's 10-cap remain


def test_admission_skips_symbols_already_rejected_today():
    """A symbol rejected on an earlier sweep today must not be re-reviewed (or
    re-backfilled) when it reappears in a later sweep's news window."""
    rec = Recorder()
    rec.rejected.add("REJCO")
    core = AnalystCore(rec.deps())
    assert core.consider_admissions([_item("REJCO")], TODAY) == []
    assert rec.admitted == []
    assert rec.backfilled == []   # never even entered the review pipeline


def test_admission_skips_llm_call_when_quant_gates_fail():
    """Every LLM (mock and real) rejects unconditionally on passed_gates=False —
    the round-trip must be skipped, not just its result discarded."""
    rec = Recorder(turnover_cr=5.0)   # below the ₹50cr gate
    llm = CountingLLM()
    core = AnalystCore(rec.deps(llm=llm))
    assert core.consider_admissions([_item("THINCO")], TODAY) == []
    assert llm.calls == 0
    assert "THINCO" in rec.rejected   # cached so tomorrow's sweep skips it too


def test_admission_llm_rejection_is_cached_for_next_sweep():
    rec = Recorder()
    core = AnalystCore(rec.deps())
    assert core.consider_admissions([_item("BADCO", sentiment="negative")], TODAY) == []
    assert "BADCO" in rec.rejected


def test_admission_backfill_failure_skips_candidates_not_cycle():
    """Caught live (Phase 5d'): AuthError from a token-less Kite backfill crashed the
    whole analyst cycle. Backfill must be best-effort: unbackfilled names fail the
    history gate individually; other candidates still get reviewed."""
    rec = Recorder(sessions=300)
    short_history = {"FRESHCO"}

    def sessions_for(sym):
        return 100 if sym in short_history else 300

    def broken_backfill(syms):
        raise RuntimeError("No Kite access token found for today.")

    deps = rec.deps(backfill_history=broken_backfill, total_sessions=sessions_for)
    core = AnalystCore(deps)
    admitted = core.consider_admissions(
        [_item("FRESHCO"), _item("SEASONED", link="https://x/seasoned")], TODAY)
    assert admitted == ["SEASONED"]          # the seasoned name still got through
    assert "admission_backfill_failed" in rec.alerts
