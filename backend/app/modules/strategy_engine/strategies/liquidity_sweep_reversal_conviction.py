"""Conviction-gated Liquidity Sweep/Reversal.

A thin subclass of `LiquiditySweepReversalStrategy` that keeps its exact
sweep-detection / confirmation-candle logic and layers the shared
cross-cutting conviction gates (`conviction_gates.ConvictionGateMixin`) on
top — same "every gate opt-in, byte-identical when off" guarantee
`orb_conviction.py` established.

**`require_volume_surge` here checks the *confirmation* bar, not the sweep
bar itself.** The originally-scoped design (a bespoke "sweep-bar-only"
volume gate) would have needed either duplicating
`LiquiditySweepReversalStrategy.check_setup`'s whole detection block just to
inject a check before `_pending_sweep` is set, or coupling this subclass to
that private internal state. Reusing the shared mixin's existing
`require_volume_surge` gate at the point `check_setup` actually returns a
`TradeProposal` (the confirmation bar, one bar after the sweep) gets the same
research-grounded intent ("a real break carries real volume, a fake one
doesn't") with zero bespoke gate code and zero base-class coupling — the
confirmation bar is adjacent to the sweep bar, not a materially different
event window. Same caveat as everywhere else this gate is used: it no-ops on
`alice_index`'s zero volume, only bites on `futures_proxy`.

Direction is resolved from the finalized `TradeProposal.option_contract_id`
via a DB lookup, same as every other `*_conviction` subclass.
`LiquiditySweepReversalStrategy` doesn't latch a fired direction (its own
docstring: "No `_fired_directions` guard, unlike ORB/OI-Volume-Confirmed") —
a gate rejection here just returns `None`, no `discard()` needed.

**2026-08-30: `min_displacement_atr` (native, this strategy only)** — an
ICT-style displacement filter: the confirmation bar's own body
(`|close - open|`), normalized by ATR14, must clear this threshold. A real
reversal is supposed to be an "impulsive move away from the level," not a
half-hearted close that barely qualifies — a small-bodied confirmation bar
is closer to noise than conviction. `0.0` (default) is off, byte-identical
to the base strategy. Sits out (returns `None`, doesn't fire) whenever
ATR14 hasn't warmed up yet, same "missing data isn't an adverse regime, but
also isn't a pass" caution `ConvictionGateMixin`'s own gates use.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import date

from sqlalchemy.orm import Session

from app.core.clock import to_ist
from app.domain.market.models import OptionContract, OptionType, PriceBar
from app.domain.strategy.models import StrategyRun
from app.modules.strategy_engine.common_rules import get_latest_indicator_value
from app.modules.strategy_engine.conviction_gates import ConvictionGateMixin
from app.modules.strategy_engine.interface import TradeProposal
from app.modules.strategy_engine.strategies.liquidity_sweep_reversal import (
    LiquiditySweepReversalStrategy,
)

logger = logging.getLogger("app.strategy_engine.liquidity_sweep_reversal_conviction")


class LiquiditySweepReversalConvictionStrategy(
    ConvictionGateMixin, LiquiditySweepReversalStrategy
):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        *,
        require_prior_day_trend: bool = False,
        prior_day_trend_buffer_pts: float = 0.0,
        vix_min: float | None = None,
        vix_max: float | None = None,
        require_atr_expansion: bool = False,
        atr_expansion_lookback: int = 20,
        atr_expansion_min_ratio: float = 1.0,
        require_volume_surge: bool = False,
        volume_surge_lookback: int = 20,
        volume_surge_min_ratio: float = 1.5,
        require_htf_ema_trend: bool = False,
        htf_ema_slope_lookback: int = 5,
        pcr_oi_min: float | None = None,
        pcr_oi_max: float | None = None,
        skip_weekdays: list[str] | None = None,
        min_displacement_atr: float = 0.0,
        **sweep_kwargs: object,
    ) -> None:
        LiquiditySweepReversalStrategy.__init__(self, instrument_id, expiry_date, **sweep_kwargs)  # type: ignore[arg-type]
        ConvictionGateMixin.__init__(
            self,
            require_prior_day_trend=require_prior_day_trend,
            prior_day_trend_buffer_pts=prior_day_trend_buffer_pts,
            vix_min=vix_min,
            vix_max=vix_max,
            require_atr_expansion=require_atr_expansion,
            atr_expansion_lookback=atr_expansion_lookback,
            atr_expansion_min_ratio=atr_expansion_min_ratio,
            require_volume_surge=require_volume_surge,
            volume_surge_lookback=volume_surge_lookback,
            volume_surge_min_ratio=volume_surge_min_ratio,
            require_htf_ema_trend=require_htf_ema_trend,
            htf_ema_slope_lookback=htf_ema_slope_lookback,
            pcr_oi_min=pcr_oi_min,
            pcr_oi_max=pcr_oi_max,
            skip_weekdays=skip_weekdays,
        )
        self.min_displacement_atr = min_displacement_atr

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        proposal = LiquiditySweepReversalStrategy.check_setup(self, db, strategy_run, latest_bar)
        if proposal is None:
            return None

        if self.min_displacement_atr > 0:
            atr = get_latest_indicator_value(db, self.instrument_id, "ATR14", self.timeframe)
            if atr is None or atr <= 0:
                return None
            displacement_atr = abs(float(latest_bar.close) - float(latest_bar.open)) / atr
            if displacement_atr < self.min_displacement_atr:
                self._log_once(
                    logger,
                    "min_displacement_atr",
                    "run %s: confirmation-bar displacement %.3f ATR below min %.3f, skipping",
                    strategy_run.id,
                    displacement_atr,
                    self.min_displacement_atr,
                )
                self.last_signal_status.candidate = proposal
                return None

        contract = db.get(OptionContract, proposal.option_contract_id)
        if contract is None:
            return None
        # See vwap_pullback_conviction.py's identical comment: a freshly-
        # queried `OptionContract.option_type` can be a raw string (plain
        # `String(2)` column, no SQLAlchemy Enum type), which would silently
        # break every `is OptionType.CE`/`is OptionType.PE` identity check
        # in `ConvictionGateMixin` -- normalize once via the enum
        # constructor (idempotent for either input type).
        option_type = OptionType(contract.option_type)

        bar_ist = to_ist(latest_bar.bucket_start)
        reject = self._conviction_reject_reason(db, latest_bar, option_type, bar_ist)
        if reject is not None:
            self._log_once(
                logger,
                f"conviction_{reject}",
                "run %s: %s setup rejected by conviction gate '%s'",
                strategy_run.id,
                option_type.value,
                reject,
            )
            self.last_signal_status.candidate = proposal
            return None

        return replace(
            proposal,
            payload={**proposal.payload, "strategy": "liquidity_sweep_reversal_conviction"},
        )
