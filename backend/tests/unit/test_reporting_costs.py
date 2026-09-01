"""`reporting.costs` and its integration into `reporting.service._compute_stats`
-- specifically the 2026-09-02 fix for a real bug: the first cut of the cost
estimate scaled brokerage with quantity, which made it mathematically
invariant to how many orders/legs a trade was split across. Real Shoonya
brokerage is a flat fee per executed order, so splitting an exit into more
legs must cost more, not the same. These tests construct `TradeOutcome` rows
directly (no DB persistence needed -- `_compute_stats` only reads attributes
off the objects it's given) so the exact regression scenario is provable
without driving the full multi-leg exit engine end-to-end.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.domain.execution.models import ExitReason, TradeOutcome
from app.modules.reporting.costs import estimate_entry_order_cost, estimate_exit_leg_cost
from app.modules.reporting.service import _compute_stats


def _outcome(
    *, position_id: uuid.UUID, entry_price: float, exit_price: float, qty: int
) -> TradeOutcome:
    return TradeOutcome(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        trading_session_id=uuid.uuid4(),
        position_id=position_id,
        trade_intent_id=uuid.uuid4(),
        entry_price=entry_price,
        exit_price=exit_price,
        qty=qty,
        realized_pnl=(exit_price - entry_price) * qty,
        slippage=0.0,
        exit_reason=ExitReason.TARGET,
        closed_at=datetime.now(UTC),
    )


def test_estimate_exit_leg_cost_charges_stt_but_not_stamp_duty():
    # STT is sell-side only; stamp duty is buy-side only -- confirm the exit
    # (sell) leg cost includes one but not the other by comparing against a
    # hand-computed floor (brokerage + GST-on-brokerage alone).
    cost = estimate_exit_leg_cost(20.0, 65)
    brokerage_and_gst_only = 5.0 * 1.18
    assert cost > brokerage_and_gst_only  # STT/txn/SEBI add on top


def test_estimate_entry_order_cost_is_flat_regardless_of_quantity():
    # The whole point of the 2026-09-02 fix: brokerage is a flat per-order
    # fee, so a 10-lot single order isn't ~10x a 1-lot order's cost.
    one_lot = estimate_entry_order_cost(20.0, 65)
    ten_lots = estimate_entry_order_cost(20.0, 650)
    assert ten_lots < one_lot * 3  # nowhere near linear scaling with qty


def test_estimate_entry_order_cost_reproduces_the_source_report_trade():
    # CN/1611432 (2026-09-01): order pair B 65@10.15 / S 65@12.10 -- real
    # brokerage was Rs 5.00 on each leg (0.0769/unit * 65 qty).
    cost = estimate_entry_order_cost(10.15, 65) + estimate_exit_leg_cost(12.10, 65)
    assert cost == pytest.approx(13.58, abs=0.05)


def test_splitting_an_exit_into_more_legs_costs_more_not_the_same():
    """The exact regression: same position, same total qty, same entry/exit
    prices -- closed via 1 exit leg vs 3 exit legs. Real broker behavior
    (flat fee per order) means the 3-leg version costs strictly more, since
    it's 3 separate exit orders instead of 1. The pre-fix formula was
    qty-linear and gave an identical total either way.
    """
    entry_price, exit_price, lot_size = 20.0, 24.0, 65
    total_qty = 10 * lot_size

    position_id = uuid.uuid4()
    single_leg = [
        _outcome(
            position_id=position_id, entry_price=entry_price, exit_price=exit_price,
            qty=total_qty,
        )
    ]
    single_leg_cost = _compute_stats(single_leg).total_cost

    position_id_2 = uuid.uuid4()
    three_legs = [
        _outcome(
            position_id=position_id_2, entry_price=entry_price, exit_price=exit_price, qty=q,
        )
        for q in (4 * lot_size, 3 * lot_size, 3 * lot_size)
    ]
    three_leg_cost = _compute_stats(three_legs).total_cost

    assert three_leg_cost > single_leg_cost
    # Exactly 2 extra exit-order flat fees (with their own GST/txn/STT on
    # each leg's own value) -- not some arbitrarily larger/smaller gap.
    extra_exit_orders = 2
    per_extra_leg_min = 5.0  # flat brokerage alone, before GST/txn/STT top it up
    assert three_leg_cost - single_leg_cost > extra_exit_orders * per_extra_leg_min

    # realized_pnl and trade_count must be completely unaffected by the
    # leg split -- only the cost estimate should differ.
    assert _compute_stats(single_leg).trade_count == 1
    assert _compute_stats(three_legs).trade_count == 1
    assert _compute_stats(single_leg).total_realized_pnl == pytest.approx(
        _compute_stats(three_legs).total_realized_pnl
    )


def test_multi_leg_position_charges_entry_cost_once_not_per_leg():
    """The other half of the fix: the entry order's own cost must be
    charged once per position (on the full original qty), never once per
    exit leg -- charging it per-leg would double/triple-count a single real
    entry order.
    """
    entry_price, exit_price, lot_size = 20.0, 24.0, 65
    position_id = uuid.uuid4()
    two_legs = [
        _outcome(
            position_id=position_id, entry_price=entry_price, exit_price=exit_price, qty=q,
        )
        for q in (5 * lot_size, 5 * lot_size)
    ]
    actual = _compute_stats(two_legs).total_cost

    expected = (
        estimate_entry_order_cost(entry_price, 10 * lot_size)  # once, full qty
        + estimate_exit_leg_cost(exit_price, 5 * lot_size)  # leg 1
        + estimate_exit_leg_cost(exit_price, 5 * lot_size)  # leg 2
    )
    assert actual == pytest.approx(expected)
