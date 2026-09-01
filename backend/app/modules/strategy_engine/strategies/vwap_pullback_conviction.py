"""Conviction-gated VWAP Pullback.

A thin subclass of `VWAPPullbackStrategy` that keeps its exact VWAP-touch /
trend-side-fraction entry logic and layers the shared cross-cutting
conviction gates (`conviction_gates.ConvictionGateMixin`) on top — same
"every gate opt-in, byte-identical when off" guarantee `orb_conviction.py`
established (a backtest of `vwap_pullback_conviction` with an empty `params`
dict reproduces the plain `vwap_pullback` baseline exactly).

Direction is resolved from the finalized `TradeProposal.option_contract_id`
via a DB lookup (`OptionContract.option_type`) rather than re-derived from
bar data — `VWAPPullbackStrategy.check_setup` already knows CE vs PE
internally (`touch_and_confirm`'s return value) but doesn't expose it on the
`TradeProposal` itself, and the DB lookup is correct regardless of which
base strategy this mixin pattern gets applied to.

`VWAPPullbackStrategy` doesn't latch a fired direction the way ORB/
OI-Volume-Confirmed do (it re-evaluates fresh every bar) — a gate rejection
here just returns `None`, no `discard()` needed.

**2026-08-30: `min_bars_since_open` (native, VWAP-only)** — a session-open
warm-up gate. Research-grounded: VWAP bands need a handful of bars to
stabilize right after session open (early-session standard deviation is
tiny, so a "touch" is trivially easy in the first few minutes). Counts
*today's* completed bars via `get_recent_completed_bars(since=day_start)`,
same day-boundary anchoring `_trend_direction` already uses. `0` (default)
is off, byte-identical to the base strategy. Likely to overlap heavily with
the existing `trend_lookback_bars=20` warm-up at default settings — the
sweep will show whether a larger value adds anything beyond that.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import date, datetime, time

from sqlalchemy.orm import Session

from app.core.clock import IST, to_ist
from app.domain.market.models import OptionContract, OptionType, PriceBar
from app.domain.strategy.models import StrategyRun
from app.modules.strategy_engine.common_rules import get_recent_completed_bars
from app.modules.strategy_engine.conviction_gates import ConvictionGateMixin
from app.modules.strategy_engine.interface import TradeProposal
from app.modules.strategy_engine.strategies.vwap_pullback import VWAPPullbackStrategy

logger = logging.getLogger("app.strategy_engine.vwap_pullback_conviction")


class VWAPPullbackConvictionStrategy(ConvictionGateMixin, VWAPPullbackStrategy):
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
        min_bars_since_open: int = 0,
        require_rsi_alignment: bool = False,
        rsi_neutral_band: float = 10.0,
        require_momentum_alignment: bool = False,
        momentum_lookback_bars: int = 1,
        **vwap_kwargs: object,
    ) -> None:
        VWAPPullbackStrategy.__init__(self, instrument_id, expiry_date, **vwap_kwargs)  # type: ignore[arg-type]
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
            require_rsi_alignment=require_rsi_alignment,
            rsi_neutral_band=rsi_neutral_band,
            require_momentum_alignment=require_momentum_alignment,
            momentum_lookback_bars=momentum_lookback_bars,
        )
        self.min_bars_since_open = min_bars_since_open

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        proposal = VWAPPullbackStrategy.check_setup(self, db, strategy_run, latest_bar)
        if proposal is None:
            return None

        if self.min_bars_since_open > 0:
            day_start = datetime.combine(
                to_ist(latest_bar.bucket_start).date(), time.min, tzinfo=IST
            )
            bars_today = get_recent_completed_bars(
                db, self.instrument_id, self.timeframe, since=day_start
            )
            if len(bars_today) < self.min_bars_since_open:
                self._log_once(
                    logger,
                    "min_bars_since_open",
                    "run %s: only %d bar(s) since session open (< %d), skipping",
                    strategy_run.id,
                    len(bars_today),
                    self.min_bars_since_open,
                )
                self.last_signal_status.candidate = proposal
                return None

        contract = db.get(OptionContract, proposal.option_contract_id)
        if contract is None:
            return None
        # `OptionContract.option_type` is declared `Mapped[OptionType]` but
        # backed by a plain `String(2)` column (no SQLAlchemy Enum type) --
        # a freshly-queried row (this `db.get`, a genuinely new SELECT in a
        # real backtest/production session, unlike a same-session ORM
        # identity-map hit) returns the raw string, not an `OptionType`
        # member. Every downstream `is OptionType.CE`/`is OptionType.PE`
        # identity check (in `ConvictionGateMixin`) would then silently
        # never match -- normalize once here via the enum constructor
        # (idempotent: accepts either a raw string or an existing member).
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
            proposal, payload={**proposal.payload, "strategy": "vwap_pullback_conviction"}
        )
