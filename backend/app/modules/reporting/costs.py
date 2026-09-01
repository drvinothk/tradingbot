"""Approximate real-money trading cost estimate for a closed options trade
-- brokerage, exchange transaction charges, STT, SEBI turnover fee, stamp
duty, and GST. Not a regulatory-precision recomputation: rates below are
effective (blended) percentages back-solved from a real Shoonya (Finvasia)
Contract Note cum Tax Invoice for this account (CN/1611432, trade date
2026-09-01, NIFTY options, account FA44103) -- reproduces that day's real
total charges (Rs 91.31 across 3 round-trip trades) to within a few paise.
Intended to give the Control Room's "Total Cost" figure a number that's
genuinely close to what the broker actually charges, not a rough guess from
public rate tables (which may lag or differ from what this specific account
is actually billed).

Assumes every trade is a long option (buy to open, sell to close) -- true
for every strategy in this codebase today (none of them write/short
options; see CLAUDE.md's Shoonya BO/CO research note). STT is charged on
the sell leg only, so this would need `Position.side` threaded through to
stay correct if a short-option strategy is ever added.
"""

from __future__ import annotations

# Rs per unit (share), per executed order leg -- printed directly on the
# source contract note as "Brokerage Per Unit (Rs)" on every single trade
# row, constant regardless of price (0.0769 for every trade from 10.15 to
# 34.65), i.e. a flat per-order fee divided by that order's quantity (65 for
# every order in the source report -- 1 lot). Doubled in the formula below
# for the two legs (entry + exit) of a round trip.
_BROKERAGE_PER_UNIT_PER_LEG = 0.0769

# Effective STT rate on the sell-side leg's value -- back-solved from the
# source report's actual STT total (Rs 13.00) against its actual combined
# sell value (Rs 8,884.75): 13.00 / 8884.75.
_STT_RATE_SELL_SIDE = 0.0014633

# NSE F&O exchange transaction charges, on total (buy + sell) turnover --
# back-solved from the source report's actual total (Rs 6.35) against its
# actual combined turnover (Rs 17,875.00): 6.35 / 17875.00. Matches NSE's
# published ~0.035% options rate closely.
_EXCHANGE_TXN_CHARGE_RATE = 0.0003553

# SEBI turnover fee, on total turnover -- back-solved the same way
# (Rs 0.02 / Rs 17,875.00), matches the published Rs 10/crore rate.
_SEBI_TURNOVER_FEE_RATE = 0.0000011

# Stamp duty on the buy-side leg's value -- the published standard rate
# (0.003%). The source report's own stamp duty rounded to Rs 0.00 for its
# small trade sizes, so this can't be back-solved from it the way the
# others above were; kept at the standard rate so a larger trade's estimate
# doesn't silently omit it.
_STAMP_DUTY_RATE = 0.00003

# GST on (brokerage + exchange transaction charges) -- exactly matches the
# source report's own "Taxable Value of Supply" definition (Rs 66.35 =
# Rs 60.00 brokerage + Rs 6.35 transaction charges) and its 18% IGST line.
_GST_RATE = 0.18


def estimate_trade_cost(entry_price: float, exit_price: float, qty: int) -> float:
    """Approximate total round-trip cost (brokerage + STT + exchange
    charges + SEBI fee + stamp duty + GST) for one closed long-option
    trade. `qty` is the absolute contract quantity (lots * lot_size), not
    lots -- same convention as `TradeOutcome.qty`.
    """
    buy_value = entry_price * qty
    sell_value = exit_price * qty
    turnover = buy_value + sell_value

    brokerage = 2 * qty * _BROKERAGE_PER_UNIT_PER_LEG
    exchange_txn_charges = turnover * _EXCHANGE_TXN_CHARGE_RATE
    stt = sell_value * _STT_RATE_SELL_SIDE
    sebi_fee = turnover * _SEBI_TURNOVER_FEE_RATE
    stamp_duty = buy_value * _STAMP_DUTY_RATE
    gst = (brokerage + exchange_txn_charges) * _GST_RATE

    return brokerage + exchange_txn_charges + stt + sebi_fee + stamp_duty + gst
