"""Signed P&L arithmetic -- the one place this formula lives.

Before this module existed, `(other_price - entry_price) * qty`, signed +1
for a long (BUY) position and -1 for a short (SELL) position, was hand-
copied in three independent places: `execution_engine.paper.service
._finalize_position_close`'s `realized_pnl` (and, identically, its
`slippage`, which is the same formula with `intended_price` swapped in for
`entry_price`), `risk_engine.service.compute_pre_trade_analytics`'s
`pnl_scenarios` table, and `api.v1.execution`'s per-position
`unrealized_pnl`. All three now call `signed_pnl` below instead of
re-deriving the sign convention -- future drift between them is
structurally impossible, not just documented as "these three must match."

Deliberately domain-independent (`app/core` doesn't import `app/domain` --
see `market_utils.py` for the same convention): `side` is accepted as a
plain string/StrEnum comparable to `"buy"`/`"sell"` rather than importing
`SignalSide`/`OrderSide` -- both of those are `enum.StrEnum` subclasses, so
passing the enum member itself works unchanged (`str(SignalSide.BUY) ==
"buy"`).
"""

from __future__ import annotations

from decimal import Decimal


def _to_decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def signed_pnl(
    entry_price: Decimal | float | str,
    other_price: Decimal | float | str,
    qty: int | Decimal,
    side: str,
) -> Decimal:
    """`(other_price - entry_price) * qty`, signed +1 for BUY/long, -1 for
    SELL/short. `other_price` is whatever is being compared against
    `entry_price` -- the actual exit fill (realized P&L), the price that
    justified the exit (slippage), or the latest tick (unrealized P&L) --
    the sign convention is identical in every case.

    Price args accept `Decimal`, `float`, or `str` and are always routed
    through `Decimal(str(x))` when not already a `Decimal` -- never a raw
    `Decimal(float)` construction, which round-trips through float's binary
    representation (see CLAUDE.md's Decimal-vs-float rule). Callers that
    already hold a `Decimal` (e.g. a `Numeric` column read back from
    Postgres) should pass it through directly rather than converting to
    `float` first, for the same reason.
    """
    sign = Decimal("1") if str(side) == "buy" else Decimal("-1")
    return (_to_decimal(other_price) - _to_decimal(entry_price)) * Decimal(qty) * sign
