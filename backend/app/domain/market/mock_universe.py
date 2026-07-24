"""Synthetic Nifty/Bank Nifty universe for mock-data development (Phase 1-4).
Produces the same `InstrumentInfo` DTOs a real broker's instrument master
would — this is the one place that knows what "a plausible Nifty option
chain" looks like, so both the mock broker adapter and DB seeding use the
same universe rather than each inventing their own.
"""

from __future__ import annotations

from datetime import date

from app.modules.broker_adapter.base.contracts import InstrumentInfo, OptionType

# (underlying symbol, exchange, lot_size, tick_size, synthetic spot price, strike step)
_UNDERLYINGS: tuple[tuple[str, str, int, float, float, float], ...] = (
    ("NIFTY", "NFO", 25, 0.05, 24500.0, 50.0),
    ("BANKNIFTY", "NFO", 15, 0.05, 52000.0, 100.0),
)


def _option_symbol(underlying: str, expiry: date, strike: float, option_type: OptionType) -> str:
    return f"{underlying}{expiry:%d%b%y}{strike:.0f}{option_type.value}".upper()


def build_mock_universe(expiry: date, strike_range: int = 10) -> list[InstrumentInfo]:
    """One underlying `InstrumentInfo` (is_option=False) per underlying, plus
    `strike_range` strikes each side of a synthetic ATM for both CE and PE.
    `strike_range=10` comfortably covers strategy 6's ATM±7 analysis window
    plus buffer, while strategies 1-5 only ever use the inner ATM±3.
    """
    universe: list[InstrumentInfo] = []

    for symbol, exchange, lot_size, tick_size, spot, strike_step in _UNDERLYINGS:
        universe.append(
            InstrumentInfo(
                symbol=symbol,
                exchange=exchange,
                lot_size=lot_size,
                tick_size=tick_size,
                is_option=False,
            )
        )

        atm_strike = round(spot / strike_step) * strike_step
        for offset in range(-strike_range, strike_range + 1):
            strike = atm_strike + offset * strike_step
            for option_type in (OptionType.CE, OptionType.PE):
                universe.append(
                    InstrumentInfo(
                        symbol=_option_symbol(symbol, expiry, strike, option_type),
                        exchange=exchange,
                        lot_size=lot_size,
                        tick_size=tick_size,
                        is_option=True,
                        underlying=symbol,
                        expiry=expiry,
                        strike=strike,
                        option_type=option_type,
                    )
                )

    return universe
