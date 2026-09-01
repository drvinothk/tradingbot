"""Approximate real-money trading cost estimate for a closed options trade
-- brokerage, exchange transaction charges, STT, SEBI turnover fee, stamp
duty, and GST. Not a regulatory-precision recomputation: the value-
proportional rates below are effective (blended) percentages back-solved
from a real Shoonya (Finvasia) Contract Note cum Tax Invoice for this
account (CN/1611432, trade date 2026-09-01, NIFTY options, account
FA44103) -- reproduces that day's real total charges (Rs 91.31 across 3
round-trip trades) to within a few paise. Intended to give the Control
Room's "Total Cost" figure a number that's genuinely close to what the
broker actually charges, not a rough guess from public rate tables (which
may lag or differ from what this specific account is actually billed).

**Brokerage is a flat fee per real broker order, not per lot/quantity.**
The source report's own "Brokerage Per Unit (Rs)" was a constant 0.0769
regardless of price on every trade -- consistent with a flat fee, but
every trade in that report also happened to be exactly 65 qty (1 lot), so
that data alone can't distinguish "flat Rs 5/order" from "Rs 0.0769/unit"
(they coincide only at qty=65). Confirmed independently (2026-09-02, user-
supplied) against Shoonya/Finvasia's own published pricing: a flat Rs 5
per executed order for intraday/F&O, explicitly *not* scaled by lot count
or quantity -- so a 10-lot order costs the same Rs 5 as a 1-lot order, and
each further leg of a staged exit is its own separate Rs 5 order. Modeled
here as `estimate_entry_order_cost`/`estimate_exit_leg_cost`, called once
per real order (see `reporting.service._collapse_to_trades`) rather than
scaled by qty -- getting this wrong previously made the estimate invariant
to how many orders a trade was split across, when in reality more orders
(e.g. a multi-leg staged exit) means more flat-fee brokerage.

Assumes every trade is a long option (buy to open, sell to close) -- true
for every strategy in this codebase today (none of them write/short
options; see CLAUDE.md's Shoonya BO/CO research note). STT/stamp duty
would need `Position.side` threaded through to stay correct if a short-
option strategy is ever added (STT is sell-side, stamp duty is buy-side --
this module currently hardcodes "buy=entry, sell=exit" accordingly).
"""

from __future__ import annotations

# Flat Rs per real executed broker order (buy or sell), regardless of
# quantity/lot count -- see module docstring.
_BROKERAGE_PER_ORDER = 5.0

# Effective STT rate on a sell order's value -- back-solved from the
# source report's actual STT total (Rs 13.00) against its actual combined
# sell value (Rs 8884.75): 13.00 / 8884.75.
_STT_RATE_SELL_SIDE = 0.0014633

# NSE F&O exchange transaction charges, on an order's own value -- back-
# solved from the source report's actual total (Rs 6.35) against its
# actual combined turnover (Rs 17875.00): 6.35 / 17875.00. Matches NSE's
# published ~0.035% options rate closely.
_EXCHANGE_TXN_CHARGE_RATE = 0.0003553

# SEBI turnover fee, on an order's own value -- back-solved the same way
# (Rs 0.02 / Rs 17875.00), matches the published Rs 10/crore rate.
_SEBI_TURNOVER_FEE_RATE = 0.0000011

# Stamp duty on a buy order's value -- the published standard rate
# (0.003%). The source report's own stamp duty rounded to Rs 0.00 for its
# small trade sizes, so this can't be back-solved from it the way the
# others above were; kept at the standard rate so a larger trade's estimate
# doesn't silently omit it.
_STAMP_DUTY_RATE = 0.00003

# GST on (brokerage + exchange transaction charges) for that same order --
# exactly matches the source report's own "Taxable Value of Supply"
# definition (Rs 66.35 = Rs 60.00 brokerage + Rs 6.35 transaction charges,
# across the day's 12 orders) and its 18% IGST line.
_GST_RATE = 0.18


def _order_cost(value: float, *, is_sell: bool, apply_stamp_duty: bool) -> float:
    """Cost of one real broker order (a buy or a sell) of the given
    notional value (price * qty)."""
    txn_charges = value * _EXCHANGE_TXN_CHARGE_RATE
    sebi_fee = value * _SEBI_TURNOVER_FEE_RATE
    stt = value * _STT_RATE_SELL_SIDE if is_sell else 0.0
    stamp_duty = value * _STAMP_DUTY_RATE if apply_stamp_duty else 0.0
    gst = (_BROKERAGE_PER_ORDER + txn_charges) * _GST_RATE
    return _BROKERAGE_PER_ORDER + txn_charges + sebi_fee + stt + stamp_duty + gst


def estimate_entry_order_cost(entry_price: float, qty: int) -> float:
    """Cost of the single real entry (buy) order that opened a position.
    Call this exactly once per position, using the position's full
    original quantity -- never per exit leg, regardless of how many legs
    that position is later closed across.
    """
    return _order_cost(entry_price * qty, is_sell=False, apply_stamp_duty=True)


def estimate_exit_leg_cost(exit_price: float, qty: int) -> float:
    """Cost of one real exit (sell) order -- one leg of a possibly staged
    multi-leg exit. Call once per `TradeOutcome` row, using that leg's own
    quantity; each leg is a genuinely separate broker order.
    """
    return _order_cost(exit_price * qty, is_sell=True, apply_stamp_duty=False)
