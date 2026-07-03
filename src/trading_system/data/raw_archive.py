"""Archive raw Kite API responses as Parquet before any transformation.

This creates a replayable source of truth that allows re-ingestion without
hitting the Kite API again. Files are written to:
  <DATA_DIR>/raw/<symbol>/<interval>/<from>_<to>.parquet
"""
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trading_system.config import get_settings
from trading_system.observability import get_logger

logger = get_logger(__name__)


def archive_raw(
    symbol: str,
    interval: str,
    from_date: date,
    to_date: date,
    records: list[dict],  # type: ignore[type-arg]
) -> Path:
    s = get_settings()
    out_dir: Path = s.data_dir / "raw" / symbol / interval
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{from_date.isoformat()}_{to_date.isoformat()}.parquet"
    out_path = out_dir / filename

    if records:
        df = pd.DataFrame(records)
        df["_archived_utc"] = pd.Timestamp.utcnow()
        df["_symbol"] = symbol
        df["_interval"] = interval
        df.to_parquet(out_path, index=False)
        logger.debug("raw_archived", symbol=symbol, interval=interval, n=len(records), path=str(out_path))
    else:
        logger.debug("raw_archive_empty", symbol=symbol, interval=interval)

    return out_path
