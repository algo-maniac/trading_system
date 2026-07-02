"""Zerodha equity charge model.

Rates verified June 2026 (https://zerodha.com/charges). Brokerage/STT/exchange/GST/
SEBI/stamp rates change over time — update the constants below with the date checked.

Two products:
- CNC  (delivery): zero brokerage, higher STT, stamp on buy.
- MIS  (intraday): capped brokerage, STT on sell only, lower stamp.

A "charge" is computed per executed order on a turnover = qty * price.
"""
from dataclasses import dataclass
from enum import Enum


class Product(str, Enum):
    CNC = "CNC"   # delivery
    MIS = "MIS"   # intraday


# --- Rate constants (fractions of turnover unless noted) — verified Jun 2026 ---
_NSE_TXN_RATE = 0.0000297        # NSE equity transaction charge ~0.00297%
_SEBI_RATE = 0.000001            # ₹10 per crore = 0.0001% ... = 0.000001 fraction
_GST_RATE = 0.18                 # 18% on (brokerage + txn + SEBI)

# CNC (delivery)
_CNC_BROKERAGE = 0.0             # zero brokerage on delivery
_CNC_STT_RATE = 0.001            # 0.1% on BOTH buy and sell
_CNC_STAMP_RATE_BUY = 0.00015    # 0.015% on buy only

# MIS (intraday)
_MIS_BROKERAGE_RATE = 0.0003     # 0.03% ...
_MIS_BROKERAGE_CAP = 20.0        # ... or ₹20 per order, whichever is lower
_MIS_STT_RATE_SELL = 0.00025     # 0.025% on sell only
_MIS_STAMP_RATE_BUY = 0.00003    # 0.003% on buy only


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: float
    stt: float
    transaction: float
    gst: float
    sebi: float
    stamp: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.transaction + self.gst + self.sebi + self.stamp


def compute_charges(
    qty: int,
    price: float,
    is_buy: bool,
    product: Product = Product.CNC,
) -> ChargeBreakdown:
    """Total Zerodha charges for one executed equity order."""
    turnover = qty * price
    if turnover <= 0:
        return ChargeBreakdown(0, 0, 0, 0, 0, 0)

    transaction = turnover * _NSE_TXN_RATE
    sebi = turnover * _SEBI_RATE

    if product == Product.CNC:
        brokerage = _CNC_BROKERAGE
        stt = turnover * _CNC_STT_RATE                       # buy and sell
        stamp = turnover * _CNC_STAMP_RATE_BUY if is_buy else 0.0
    else:  # MIS
        brokerage = min(turnover * _MIS_BROKERAGE_RATE, _MIS_BROKERAGE_CAP)
        stt = 0.0 if is_buy else turnover * _MIS_STT_RATE_SELL
        stamp = turnover * _MIS_STAMP_RATE_BUY if is_buy else 0.0

    gst = (brokerage + transaction + sebi) * _GST_RATE

    return ChargeBreakdown(
        brokerage=brokerage,
        stt=stt,
        transaction=transaction,
        gst=gst,
        sebi=sebi,
        stamp=stamp,
    )
