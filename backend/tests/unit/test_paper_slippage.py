"""`_apply_slippage` — the direction-of-slippage rule shared by
`dispatch_trade_intent` (entry) and `close_position` (exit): a BUY order
must fill slightly worse (higher) than the reference price, a SELL order
slightly worse (lower) — same rule regardless of whether the order is an
entry or an exit, see the function's own docstring.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.strategy.models import SignalSide
from app.modules.execution_engine.paper.service import _apply_slippage


def test_zero_slippage_returns_the_price_unchanged():
    assert _apply_slippage(Decimal("100.0"), SignalSide.BUY, Decimal("0")) == Decimal("100.0")
    assert _apply_slippage(Decimal("100.0"), SignalSide.SELL, Decimal("0")) == Decimal("100.0")


def test_buy_fills_slightly_higher():
    result = _apply_slippage(Decimal("100.0"), SignalSide.BUY, Decimal("0.01"))
    assert result == Decimal("101.00")


def test_sell_fills_slightly_lower():
    result = _apply_slippage(Decimal("100.0"), SignalSide.SELL, Decimal("0.01"))
    assert result == Decimal("99.00")
