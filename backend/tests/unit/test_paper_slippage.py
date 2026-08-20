"""`_apply_slippage` — the direction-of-slippage rule shared by
`dispatch_trade_intent` (entry) and `close_position` (exit): a BUY order
must fill slightly worse (higher) than the reference price, a SELL order
slightly worse (lower) — same rule regardless of whether the order is an
entry or an exit, see the function's own docstring.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.strategy.models import SignalSide
from app.modules.execution_engine.paper.service import _apply_slippage, _round_to_tick


def test_zero_slippage_returns_the_price_unchanged():
    assert _apply_slippage(Decimal("100.0"), SignalSide.BUY, Decimal("0")) == Decimal("100.0")
    assert _apply_slippage(Decimal("100.0"), SignalSide.SELL, Decimal("0")) == Decimal("100.0")


def test_buy_fills_slightly_higher():
    result = _apply_slippage(Decimal("100.0"), SignalSide.BUY, Decimal("0.01"))
    assert result == Decimal("101.00")


def test_sell_fills_slightly_lower():
    result = _apply_slippage(Decimal("100.0"), SignalSide.SELL, Decimal("0.01"))
    assert result == Decimal("99.00")


def test_round_to_tick_leaves_an_already_aligned_price_unchanged():
    assert _round_to_tick(Decimal("101.00"), Decimal("0.05"), SignalSide.BUY) == Decimal("101.00")
    assert _round_to_tick(Decimal("101.00"), Decimal("0.05"), SignalSide.SELL) == Decimal("101.00")


def test_round_to_tick_rounds_a_buy_up_to_the_next_tick():
    result = _round_to_tick(Decimal("101.01"), Decimal("0.05"), SignalSide.BUY)
    assert result == Decimal("101.05")


def test_round_to_tick_rounds_a_sell_down_to_the_previous_tick():
    result = _round_to_tick(Decimal("98.99"), Decimal("0.05"), SignalSide.SELL)
    assert result == Decimal("98.95")


def test_round_to_tick_with_zero_tick_size_returns_the_price_unchanged():
    assert _round_to_tick(Decimal("101.03"), Decimal("0"), SignalSide.BUY) == Decimal("101.03")
