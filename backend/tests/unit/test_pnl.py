"""`app.core.pnl.signed_pnl` -- the single shared home for the sign/qty
formula that used to be hand-copied in three places (`execution_engine
.paper.service._finalize_position_close`'s realized_pnl/slippage,
`risk_engine.service.compute_pre_trade_analytics`'s pnl_scenarios,
`api.v1.execution`'s unrealized_pnl). See that module's own docstring.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.pnl import signed_pnl


def test_buy_side_is_long_price_up_is_profit():
    assert signed_pnl(80.0, 92.0, 25, "buy") == Decimal("300")


def test_buy_side_is_long_price_down_is_loss():
    assert signed_pnl(80.0, 72.0, 25, "buy") == Decimal("-200")


def test_sell_side_is_short_price_down_is_profit():
    assert signed_pnl(80.0, 72.0, 25, "sell") == Decimal("200")


def test_sell_side_is_short_price_up_is_loss():
    assert signed_pnl(80.0, 92.0, 25, "sell") == Decimal("-300")


def test_accepts_a_stresnum_side_value_directly():
    # SignalSide/OrderSide are both enum.StrEnum with value "buy"/"sell" --
    # passing the enum member itself (not str(...)'d by the caller) must
    # work unchanged, since every real call site does exactly this.
    import enum

    class _Side(enum.StrEnum):
        BUY = "buy"
        SELL = "sell"

    assert signed_pnl(80.0, 92.0, 25, _Side.BUY) == Decimal("300")
    assert signed_pnl(80.0, 92.0, 25, _Side.SELL) == Decimal("-300")


def test_accepts_decimal_inputs_without_a_float_round_trip():
    # Numeric/Decimal columns read back from Postgres must be passable
    # directly -- Decimal(str(x)) round-tripping via float is exactly the
    # trap CLAUDE.md's Decimal-vs-float rule warns about, so a Decimal
    # input must produce an exact result, not one perturbed by binary-float
    # representation error.
    entry = Decimal("80.05")
    exit_ = Decimal("92.35")
    result = signed_pnl(entry, exit_, 25, "buy")
    assert result == (exit_ - entry) * Decimal(25)
    assert isinstance(result, Decimal)


def test_zero_qty_is_zero_pnl():
    assert signed_pnl(80.0, 92.0, 0, "buy") == Decimal("0")


def test_same_formula_used_for_slippage_by_swapping_entry_for_intended_price():
    # execution_engine.paper.service._finalize_position_close's slippage is
    # the identical formula with intended_price standing in for entry_price
    # -- confirms both callers of signed_pnl in that function stay
    # consistent with each other.
    intended = 75.0
    fill = 74.5
    assert signed_pnl(intended, fill, 25, "buy") == Decimal("-12.5")
