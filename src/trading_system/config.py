from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# NIFTY 50 constituent tradingsymbols on NSE.
# Last verified against Kite instrument dump: June 2026.
# Check current composition at: https://www.nseindia.com/products-services/indices-nifty50-index
# To add a symbol: ensure it appears in `instruments` table (run sync-instruments first),
# then add its tradingsymbol here.
NIFTY_50_SYMBOLS: list[str] = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BPCL", "BHARTIARTL",
    "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "ETERNAL",   # ETERNAL = Zomato Ltd (rebranded 2025, replaced LTIM)
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC",
    "INDUSINDBK", "INFY", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC",
    "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
    "SUNPHARMA", "TATACONSUM", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "ULTRACEMCO", "WIPRO", "SHRIRAMFIN",
    # TODO: Confirm TATAMOTORS replacement from NSE factsheet (candidates: BEL, JIOFIN).
    # Check: https://www.nseindia.com/products-services/indices-nifty50-index
    # Then verify the symbol exists: SELECT tradingsymbol FROM instruments WHERE tradingsymbol='BEL';
    "BEL",    # remove if not in current NIFTY 50
]

# The most liquid NIFTY names — deep books, tight spreads — where intraday strategies are
# viable and the simple slippage model stays realistic. The default intraday universe.
LIQUID_SYMBOLS: list[str] = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "SBIN", "AXISBANK", "KOTAKBANK", "ITC", "LT",
    "BHARTIARTL", "HINDUNILVR", "BAJFINANCE",
]

# NIFTY 200 constituents (official NSE list, fetched 2026-07). The deployed swing/momentum
# book universe: broad enough to diversify cross-sectional momentum's single-name
# concentration risk (Phase 5R finding) without going so wide that liquidity/slippage
# assumptions break down. Superset of NIFTY_50_SYMBOLS.
NIFTY_200_SYMBOLS: list[str] = [
    "360ONE", "ABB", "APLAPOLLO", "AUBANK", "ADANIENSOL",
    "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "ATGL",
    "ABCAPITAL", "ALKEM", "AMBUJACEM", "APOLLOHOSP", "ASHOKLEY",
    "ASIANPAINT", "ASTRAL", "AUROPHARMA", "DMART", "AXISBANK",
    "BSE", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BAJAJHLDNG",
    "BANKBARODA", "BANKINDIA", "BDL", "BEL", "BHARATFORG",
    "BHEL", "BPCL", "BHARTIARTL", "GROWW", "BIOCON",
    "BLUESTARCO", "BOSCHLTD", "BRITANNIA", "CGPOWER", "CANBK",
    "CHOLAFIN", "CIPLA", "COALINDIA", "COCHINSHIP", "COFORGE",
    "COLPAL", "CONCOR", "COROMANDEL", "CUMMINSIND", "DLF",
    "DABUR", "DIVISLAB", "DIXON", "DRREDDY", "EICHERMOT",
    "ETERNAL", "EXIDEIND", "NYKAA", "FEDERALBNK", "FORTIS",
    "GAIL", "GVT&D", "GMRAIRPORT", "GLENMARK", "GODFRYPHLP",
    "GODREJCP", "GODREJPROP", "GRASIM", "HCLTECH", "HDFCAMC",
    "HDFCBANK", "HDFCLIFE", "HAVELLS", "HEROMOTOCO", "HINDALCO",
    "HAL", "HINDPETRO", "HINDUNILVR", "HINDZINC", "POWERINDIA",
    "HUDCO", "HYUNDAI", "ICICIBANK", "ICICIGI", "ICICIAMC",
    "IDFCFIRSTB", "ITC", "INDIANB", "INDHOTEL", "IOC",
    "IRCTC", "IRFC", "IREDA", "INDUSTOWER", "INDUSINDBK",
    "NAUKRI", "INFY", "INDIGO", "JSWENERGY", "JSWSTEEL",
    "JINDALSTEL", "JIOFIN", "JUBLFOOD", "KEI", "KPITTECH",
    "KALYANKJIL", "KOTAKBANK", "LTF", "LGEINDIA", "LICHSGFIN",
    "LTM", "LT", "LAURUSLABS", "LENSKART", "LODHA",
    "LUPIN", "MRF", "M&MFIN", "M&M", "MANKIND",
    "MARICO", "MARUTI", "MFSL", "MAXHEALTH", "MAZDOCK",
    "MOTILALOFS", "MPHASIS", "MCX", "MUTHOOTFIN", "NHPC",
    "NMDC", "NTPC", "NATIONALUM", "NESTLEIND", "OBEROIRLTY",
    "ONGC", "OIL", "PAYTM", "OFSS", "POLICYBZR",
    "PIIND", "PAGEIND", "PATANJALI", "PERSISTENT", "PHOENIXLTD",
    "PIDILITIND", "POLYCAB", "PFC", "POWERGRID", "PREMIERENE",
    "PRESTIGE", "PNB", "RECLTD", "RADICO", "RVNL",
    "RELIANCE", "SBICARD", "SBILIFE", "SRF", "MOTHERSON",
    "SHREECEM", "SHRIRAMFIN", "ENRIN", "SIEMENS", "SOLARINDS",
    "SBIN", "SAIL", "SUNPHARMA", "SUPREMEIND", "SUZLON",
    "SWIGGY", "TVSMOTOR", "TATACAP", "TATACOMM", "TCS",
    "TATACONSUM", "TATAELXSI", "TATAINVEST", "TMCV", "TMPV",
    "TATAPOWER", "TATASTEEL", "TECHM", "TITAN", "TORNTPHARM",
    "TRENT", "TIINDIA", "UPL", "ULTRACEMCO", "UNIONBANK",
    "UNITDSPR", "VBL", "VEDL", "VMM", "IDEA",
    "VOLTAS", "WAAREEENER", "WIPRO", "YESBANK", "ZYDUSLIFE",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Kite Connect
    kite_api_key: str = ""
    kite_api_secret: str = ""

    # Infrastructure
    database_url: str = "postgresql://trading:trading_pass@localhost:5432/trading_db"
    redis_url: str = "redis://localhost:6379/0"

    # Local data archive
    data_dir: Path = Path("./data")

    # Logging
    log_level: str = "INFO"

    # Universe
    nifty_50_symbols: list[str] = Field(default_factory=lambda: NIFTY_50_SYMBOLS)
    default_intervals: list[str] = Field(default_factory=lambda: ["day", "minute"])
    # Liquid subset for intraday work — most-traded NIFTY names where intraday edges exist
    # and slippage assumptions hold. Used as the default universe for the Reader/Backtester agents.
    liquid_symbols: list[str] = Field(default_factory=lambda: LIQUID_SYMBOLS)
    # Deployed swing/momentum book universe (Phase 5R) — see NIFTY_200_SYMBOLS for rationale.
    nifty_200_symbols: list[str] = Field(default_factory=lambda: NIFTY_200_SYMBOLS)

    # Reader agent (scheduled incremental ingestion)
    # Default = the swing sleeves' EOD day-bar path (Nifty 200 + v3 features, publishes
    # after close). Intraday 5-min data comes from the WebSocket live_app, not this.
    reader_intervals: list[str] = Field(default_factory=lambda: ["day"])
    # how far back each scheduled pull reaches; overlap is safe (upserts are idempotent)
    reader_lookback_minutes: int = 90
    # Day mode: poll every 10 minutes; the day-mode session window (15:45-23:00 IST)
    # gates when work actually happens, so this is cheap no-ops the rest of the day.
    # Deliberately NOT a single precise "45 15 * * *" shot — that has no retry if one
    # invocation is missed or fails, silently losing the whole day's swing-sleeve feed.
    reader_schedule_cron: str = "*/10 * * * *"
    # how often to re-check for a fresh token after a token_expired alert
    reader_token_poll_seconds: float = 30.0

    # Bus (Redis Streams) — stream keys + caps
    bus_bar_stream: str = "market.bars"
    bus_alert_stream: str = "alerts"
    bus_signal_stream: str = "trade.signals"
    bus_maxlen: int = 500_000               # approximate cap (XADD MAXLEN ~) on the bar stream

    # Paper-trading book (Phase 5a) — sleeve capital fractions of paper_capital.
    # These are the Phase 5R/5V validated sleeves; params live in the strategy defaults
    # plus the overrides in agents/strategist/app.py's SLEEVES.
    paper_capital: float = 1_000_000.0
    paper_sleeve_weights: dict[str, float] = Field(
        default_factory=lambda: {"momentum": 0.30, "stabilizer": 0.50, "event_intraday": 0.20}
    )
    paper_slippage_bps: float = 5.0

    # Kite API behaviour
    kite_historical_rps: float = 3.0       # max requests per second (Zerodha hard limit)
    kite_retry_max: int = 5                # max retry attempts per chunk
    kite_retry_base_delay: float = 1.0     # seconds; doubles on each retry
    ingest_workers: int = 5                # parallel symbol workers; all share the rate limiter

    # Backtest defaults (env-overridable; detailed risk/strategy params live in their modules)
    backtest_initial_capital: float = 1_000_000.0   # ₹10 lakh
    backtest_slippage_bps: float = 5.0              # basis points, adverse to fill direction

    # --- Phase 5c: news-driven analysis (Analyst agent) ---
    stockinsights_api_key: str = ""
    # Zuplo-gateway host (per docs; matches the zpka_ key prefix), env-overridable in
    # case StockInsights moves to a vanity domain later.
    stockinsights_base_url: str = "https://stockinsights-ai-main-95a26a0.zuplo.app"
    stockinsights_daily_budget: int = 100   # trial quota unknown → conservative cap
    stockinsights_rps: float = 1.0          # politeness limit, not a documented cap
    llm_provider: str = "mock"              # "mock" until the Claude API plan is purchased
    llm_daily_call_budget: int = 100
    admission_max_new_per_day: int = 10     # caps Kite backfill burst from news names
    # Analyst sweep schedule (IST): evening filings sweeps + pre-open sweep; the focus
    # build runs as part of the 08:15 sweep (after it, same cycle).
    analyst_sweep_cron: str = "15 8,18,21 * * *"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
