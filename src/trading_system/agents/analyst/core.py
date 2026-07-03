"""AnalystCore: pure orchestration of sweep → admission → focus-list, all deps injected.

Mirrors ReaderScheduler's testing discipline: every external effect (news API, DB
writes, Kite backfill, LLM, ledger) is an injected callable, so the whole daily
decision flow is unit-testable without any network or database.

Admission failure semantics: the LLM stage fails CLOSED (an un-reviewable name is not
added — the opposite of the future signal-gate, which fails open, because HERE the
conservative action is "don't expand the universe"). A BudgetExhausted news sweep
degrades to "no new news today" + alert; it never crashes the agent.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from trading_system.data.stockinsights_client import NewsItem
from trading_system.llm.base import AdmissionContext, LLMClient, Verdict
from trading_system.net.limits import BudgetExhausted
from trading_system.observability import get_logger

logger = get_logger(__name__)

_FOCUS_CAP = 50
_TURNOVER_GATE_CR = 50.0
_CRORE = 1e7
# History gates: momentum_252_21 needs 252 sessions + ranking margin; swing needs
# SMA200 + margin. One admission gate at the stricter bound keeps it simple — a name
# either has a season of tradable history or it doesn't.
_MIN_SESSIONS = 260


@dataclass
class AnalystDeps:
    """Injectable effects. app.py wires the real ones; tests wire fakes."""
    fetch_news: Callable[[date, date], list[NewsItem]]
    store_news: Callable[[list[NewsItem]], int]
    news_id_lookup: Callable[[str], "int | None"]
    upsert_gap_watch: Callable[[date, str, str, "int | None"], None]
    known_nse_symbols: Callable[[list[str]], set]        # instruments-table resolution
    backfill_history: Callable[[list[str]], None]        # 2y day bars + features
    quant_stats: Callable[[str], "dict | None"]          # median_turnover, sessions, avg_range_pct
    total_sessions: Callable[[str], int]
    llm: LLMClient = None                                # type: ignore[assignment]
    admit_member: Callable[[str, dict], bool] = None     # type: ignore[assignment]
    # The already-tradable swing universe (Nifty 200) — names here are NEVER admission
    # candidates; they're tradable by construction (caught live: NESTLEIND was
    # "admitted" because this exclusion was missing).
    base_universe: Callable[[], list] = field(default_factory=lambda: list)   # type: ignore[assignment]
    active_members: Callable[[], list] = field(default_factory=lambda: list)  # type: ignore[assignment]
    volatile_screen: Callable[[], list] = field(default_factory=lambda: list)  # type: ignore[assignment]
    liquid_symbols: Callable[[], list] = field(default_factory=lambda: list)  # type: ignore[assignment]
    held_symbols: Callable[[], list] = field(default_factory=lambda: list)    # type: ignore[assignment]
    write_focus: Callable[[date, dict], int] = None      # type: ignore[assignment]
    alert: Callable[[str, str], None] = None             # type: ignore[assignment]
    admission_cap_per_day: int = 10


class AnalystCore:
    def __init__(self, deps: AnalystDeps) -> None:
        self.d = deps
        self._admissions_today: tuple[date, int] | None = None

    # --- sweep --------------------------------------------------------------------

    def run_sweep(self, now: datetime) -> dict:  # type: ignore[type-arg]
        """Fetch + store the last ~36h of announcements; refresh the gap watchlist."""
        try:
            items = self.d.fetch_news(now.date() - timedelta(days=1), now.date())
        except BudgetExhausted as exc:
            self.d.alert("news_budget_exhausted", str(exc))
            logger.warning("sweep_skipped_budget")
            return {"status": "budget_exhausted", "fetched": 0, "new": 0}
        new = self.d.store_news(items)
        watch = 0
        for item in items:
            if item.nse_symbol and self._is_material(item):
                direction = "down" if item.sentiment == "negative" else "up"
                self.d.upsert_gap_watch(
                    now.date() + timedelta(days=1) if now.hour >= 15 else now.date(),
                    item.nse_symbol, direction,
                    self.d.news_id_lookup(item.source_link_hash),
                )
                watch += 1
        logger.info("sweep_done", fetched=len(items), new=new, gap_watch=watch)
        return {"status": "ok", "fetched": len(items), "new": new, "gap_watch": watch,
                "items": items}

    @staticmethod
    def _is_material(item: NewsItem) -> bool:
        """Cheap materiality pre-filter; the LLM review is the real judge for admissions.

        StockInsights' own `significance` flag is the primary signal (their model read
        the filing); non-neutral sentiment is the secondary net for anything it missed.
        """
        return item.significant or item.sentiment in ("positive", "negative")

    # --- admission ------------------------------------------------------------------

    def consider_admissions(self, items: list[NewsItem], today: date) -> list[str]:
        """News-discovered symbols not yet tradable → the admission pipeline."""
        current = set(self.d.base_universe()) | set(self.d.liquid_symbols()) \
            | set(self.d.active_members()) | set(self.d.volatile_screen())
        candidates: list[NewsItem] = []
        seen: set = set()
        for item in items:
            sym = item.nse_symbol
            if not sym or sym in current or sym in seen or not self._is_material(item):
                continue
            seen.add(sym)
            candidates.append(item)

        known = self.d.known_nse_symbols([c.nse_symbol for c in candidates])  # type: ignore[misc]
        cap = self._remaining_cap(today)
        admitted: list[str] = []
        to_review = [c for c in candidates if c.nse_symbol in known][:cap]
        need_history = [c.nse_symbol for c in to_review
                        if self.d.total_sessions(c.nse_symbol) < _MIN_SESSIONS]  # type: ignore[arg-type]
        if need_history:
            self.d.backfill_history(need_history)  # type: ignore[arg-type]

        for item in to_review:
            sym = item.nse_symbol
            stats = self.d.quant_stats(sym)  # type: ignore[arg-type]
            sessions = self.d.total_sessions(sym)  # type: ignore[arg-type]
            passed = bool(
                stats is not None
                and stats["median_turnover"] >= _TURNOVER_GATE_CR * _CRORE
                and sessions >= _MIN_SESSIONS
            )
            quant = {
                "median_turnover_cr": (stats["median_turnover"] / _CRORE) if stats else 0.0,
                "sessions": sessions,
                "avg_range_pct": stats["avg_range_pct"] if stats else 0.0,
                "passed_gates": passed,
            }
            verdict: Verdict = self.d.llm.review_admission(AdmissionContext(
                symbol=sym, company_name=item.company_name,  # type: ignore[arg-type]
                announcement_type=item.announcement_type, sentiment=item.sentiment,
                summary_header=item.summary_header, summary_text=item.summary_text,
                quant=quant,
            ))
            logger.info("admission_review", symbol=sym, admit=verdict.admit,
                        confidence=verdict.confidence, reasoning=verdict.reasoning)
            if verdict.admit and self.d.admit_member(sym, {  # type: ignore[misc]
                "news": item.source_link, "quant": quant,
                "verdict": {"admit": verdict.admit, "confidence": verdict.confidence,
                            "reasoning": verdict.reasoning, "reviewer": self.d.llm.name},
            }):
                admitted.append(sym)  # type: ignore[arg-type]
                self._count_admission(today)
        if admitted:
            self.d.alert("universe_admissions", ",".join(admitted))
        return admitted

    def _remaining_cap(self, today: date) -> int:
        day, used = self._admissions_today or (today, 0)
        if day != today:
            used = 0
        return max(0, self.d.admission_cap_per_day - used)

    def _count_admission(self, today: date) -> None:
        day, used = self._admissions_today or (today, 0)
        self._admissions_today = (today, used + 1 if day == today else 1)

    # --- focus list -------------------------------------------------------------------

    def build_focus(self, today: date, news_symbols: list[str],
                    gap_symbols: list[str]) -> dict:  # type: ignore[type-arg]
        """The day's intraday watch: priority-ordered union, capped, held ALWAYS kept.

        Priority (survives the cap in this order): held positions (exits must stay
        live) > liquid anchor > gap watch > news > admitted members > volatile screen.
        """
        ordered: list[tuple[str, str]] = (
            [(s, "held") for s in self.d.held_symbols()]
            + [(s, "liquid") for s in self.d.liquid_symbols()]
            + [(s, "gap_watch") for s in gap_symbols]
            + [(s, "news") for s in news_symbols]
            + [(s, "admitted") for s in self.d.active_members()]
            + [(s, "volatile_screen") for s in self.d.volatile_screen()]
        )
        entries: dict[str, list[str]] = {}
        for symbol, source in ordered:
            if symbol in entries:
                if source not in entries[symbol]:
                    entries[symbol].append(source)
            elif len(entries) < _FOCUS_CAP:
                entries[symbol] = [source]
        self.d.write_focus(today, entries)  # type: ignore[misc]
        logger.info("focus_built", date=str(today), n=len(entries))
        return entries
