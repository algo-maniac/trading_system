"""Zerodha charge model tests — values checked against hand computation."""
import pytest

from trading_system.execution.fees import Product, compute_charges


def test_cnc_buy_charges():
    # 100 shares @ ₹1000 = ₹100,000 turnover, delivery buy
    c = compute_charges(100, 1000.0, is_buy=True, product=Product.CNC)
    assert c.brokerage == 0.0                      # zero brokerage on delivery
    assert c.stt == pytest.approx(100.0)           # 0.1% of 100k
    assert c.transaction == pytest.approx(2.97)    # 0.00297% of 100k
    assert c.stamp == pytest.approx(15.0)          # 0.015% of 100k (buy only)
    assert c.sebi == pytest.approx(0.1)            # ₹10/cr
    assert c.gst == pytest.approx((0 + 2.97 + 0.1) * 0.18)
    assert c.total > 0


def test_cnc_sell_no_stamp():
    c = compute_charges(100, 1000.0, is_buy=False, product=Product.CNC)
    assert c.stamp == 0.0                           # stamp on buy only
    assert c.stt == pytest.approx(100.0)            # STT both sides for delivery


def test_mis_brokerage_capped_at_20():
    # Large turnover → 0.03% would exceed ₹20, so brokerage caps at 20
    c = compute_charges(1000, 1000.0, is_buy=True, product=Product.MIS)  # ₹1,000,000 turnover
    assert c.brokerage == pytest.approx(20.0)       # min(0.03% * 1M = 300, 20) = 20


def test_mis_brokerage_below_cap():
    # Small turnover → 0.03% is below ₹20
    c = compute_charges(10, 100.0, is_buy=True, product=Product.MIS)  # ₹1000 turnover
    assert c.brokerage == pytest.approx(0.3)        # 0.03% of 1000


def test_mis_stt_sell_only():
    buy = compute_charges(100, 1000.0, is_buy=True, product=Product.MIS)
    sell = compute_charges(100, 1000.0, is_buy=False, product=Product.MIS)
    assert buy.stt == 0.0                            # no STT on intraday buy
    assert sell.stt == pytest.approx(25.0)           # 0.025% of 100k


def test_zero_turnover():
    c = compute_charges(0, 1000.0, is_buy=True)
    assert c.total == 0.0


def test_total_is_sum_of_components():
    c = compute_charges(50, 2500.0, is_buy=True, product=Product.CNC)
    assert c.total == pytest.approx(
        c.brokerage + c.stt + c.transaction + c.gst + c.sebi + c.stamp
    )
