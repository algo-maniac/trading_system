"""Unit tests for data cleaning — no live DB or API needed."""
import numpy as np
import pytest

from tests.conftest import make_raw_kite
from trading_system.data.cleaning import clean_ohlcv


def test_clean_happy_path():
    raw = make_raw_kite(10)
    result = clean_ohlcv(raw, 12345, "minute", "TEST")
    assert len(result.df) == 10
    assert result.duplicate_count == 0
    assert result.rejected_rows == 0


def test_timestamps_converted_to_utc():
    raw = make_raw_kite(5)
    result = clean_ohlcv(raw, 12345, "minute", "TEST")
    assert str(result.df["ts"].dt.tz) == "UTC"


def test_dedup_removes_exact_duplicates():
    raw = make_raw_kite(5)
    result = clean_ohlcv(raw + raw, 12345, "minute", "TEST")
    assert len(result.df) == 5
    assert result.duplicate_count == 5


def test_ohlc_sanity_rejects_bad_high():
    raw = make_raw_kite(5)
    raw[2]["high"] = raw[2]["open"] - 50  # high < open: invalid
    result = clean_ohlcv(raw, 12345, "minute", "TEST")
    assert result.rejected_rows == 1
    assert len(result.df) == 4


def test_ohlc_sanity_rejects_bad_low():
    raw = make_raw_kite(5)
    raw[1]["low"] = raw[1]["close"] + 50  # low > close: invalid
    result = clean_ohlcv(raw, 12345, "minute", "TEST")
    assert result.rejected_rows == 1


def test_negative_volume_rejected():
    raw = make_raw_kite(5)
    raw[0]["volume"] = -1
    result = clean_ohlcv(raw, 12345, "minute", "TEST")
    assert result.rejected_rows == 1


def test_empty_input():
    result = clean_ohlcv([], 12345, "minute", "TEST")
    assert result.df.empty
    assert result.rejected_rows == 0
    assert result.gap_count == 0
    assert result.duplicate_count == 0


def test_metadata_columns_added():
    raw = make_raw_kite(5)
    result = clean_ohlcv(raw, 99999, "minute", "TEST")
    assert "instrument_token" in result.df.columns
    assert (result.df["instrument_token"] == 99999).all()
    assert (result.df["interval"] == "minute").all()
