"""Conviction-gated EMA Micro-pullback.

A thin subclass of `EMAMicroPullbackStrategy` that keeps its exact
expansion/bone-zone/body-ratio entry logic and layers the shared
cross-cutting conviction gates (`conviction_gates.ConvictionGateMixin`) on
top — same "every gate opt-in, byte-identical when off" guarantee
`orb_conviction.py` established.

`require_htf_ema_trend` is a real, usable param here (the mixin doesn't know
which base strategy it's mixed into) but is deliberately left off every
sweep config for this strategy — it would largely duplicate
`EMAMicroPullbackStrategy`'s own EMA9/EMA20 expansion filter, which already
requires an accelerating, same-signed spread on the identical indicators.

Direction is resolved from the finalized `TradeProposal.option_contract_id`
via a DB lookup, same as every other `*_conviction` subclass.
`EMAMicroPullbackStrategy` doesn't latch a fired direction — a gate
rejection here just returns `None`, no `discard()` needed.

**2026-08-30: `min_ema_spread_atr_ratio` (native, this strategy only)** — a
trend-strength filter in the spirit of an ADX threshold (research: ADX > 20
filters weak/sideways regimes before trusting a pullback entry), built from
indicators this codebase already computes rather than a new ADX pipeline:
`|EMA9 - EMA20| / ATR14` at the entry bar must clear this ratio. The base
strategy's own expansion filter already requires the spread to be
monotonically *widening*; this additionally requires it to already be
*wide relative to volatility* — a trend that's barely separated from noise
can still technically satisfy "widening" while adding one point per bar
from a near-zero base. `0.0` (default) is off, byte-identical to the base
strategy. Sits out when ATR14 hasn't warmed up yet, same convention as
`liquidity_sweep_reversal_conviction.py`'s `min_displacement_atr`.
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
from app.modules.strategy_engine.strategies.ema_micro_pullback import EMAMicroPullbackStrategy

logger = logging.getLogger("app.strategy_engine.ema_micro_pullback_conviction")


class EMAMicroPullbackConvictionStrategy(ConvictionGateMixin, EMAMicroPullbackStrategy):
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
        min_ema_spread_atr_ratio: float = 0.0,
        **ema_kwargs: object,
    ) -> None:
        EMAMicroPullbackStrategy.__init__(self, instrument_id, expiry_date, **ema_kwargs)  # type: ignore[arg-type]
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
        self.min_ema_spread_atr_ratio = min_ema_spread_atr_ratio

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        proposal = EMAMicroPullbackStrategy.check_setup(self, db, strategy_run, latest_bar)
        if proposal is None:
            return None

        if self.min_ema_spread_atr_ratio > 0:
            atr = get_latest_indicator_value(db, self.instrument_id, "ATR14", self.timeframe)
            ema9 = proposal.payload.get("ema9")
            ema20 = proposal.payload.get("ema20")
            if atr is None or atr <= 0 or ema9 is None or ema20 is None:
                return None
            spread_atr = abs(float(ema9) - float(ema20)) / atr
            if spread_atr < self.min_ema_spread_atr_ratio:
                self._log_once(
                    logger,
                    "min_ema_spread_atr_ratio",
                    "run %s: EMA9-EMA20 spread %.3f ATR below min %.3f, skipping",
                    strategy_run.id,
                    spread_atr,
                    self.min_ema_spread_atr_ratio,
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
            proposal, payload={**proposal.payload, "strategy": "ema_micro_pullback_conviction"}
        )
