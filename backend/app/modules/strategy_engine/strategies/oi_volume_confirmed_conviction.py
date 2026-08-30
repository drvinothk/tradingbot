"""Conviction-gated OI/Volume Confirmed.

A thin subclass of `OIVolumeConfirmedStrategy` that keeps its exact rolling-
window breakout / false-breakout / chain-participation-weighted ranking
logic and layers the shared cross-cutting conviction gates
(`conviction_gates.ConvictionGateMixin`) on top — same "every gate opt-in,
byte-identical when off" guarantee `orb_conviction.py` established.

Direction is resolved from the finalized `TradeProposal.option_contract_id`
via a DB lookup, same as every other `*_conviction` subclass.

**Unlike VWAP Pullback/EMA Micro-pullback, this one needs the same
`_fired_directions.discard()` undo `orb_conviction.py` needs**:
`OIVolumeConfirmedStrategy.check_setup` already adds `candidate` to
`self._fired_directions` (permanently latching that direction for the rest
of the run) *before* returning the `TradeProposal` — by the time this
subclass's conviction gate sees the proposal, the base class has already
committed to it. A gate rejection here must undo that latch so a later,
better-qualifying bar can re-fire the same direction; without the discard,
one conviction-rejected breakout would silently and permanently block that
direction for the rest of the run, same failure mode `orb_conviction.py`'s
own module docstring already documents for ORB.

**2026-08-30: `require_oi_price_alignment` / `oi_alignment_lookback_bars`
(native, this strategy only)** — a positioning-direction confirmation,
research-grounded ("if a breakout happens with falling OI, it's likely
false" / rising-price-plus-rising-OI = genuine long buildup). The base
strategy's own `oi_use_atm_oi_buildup` stub was never wired to a real
temporal-OI pipeline (per-contract OI history isn't tracked anywhere
today, and building that is a bigger lift than this round's time budget) —
this uses the chain-wide PCR instead, which every bar's `env` payload
already carries, as a cheaper but directionally equivalent proxy: PCR
*falling* over the lookback window means call-side OI is building faster
than put-side (or put OI is unwinding) — a bullish positioning shift that
should back a CE breakout; PCR *rising* is the symmetric bearish shift for
a PE breakout. A rolling `(bar_count, pcr_oi)` history is appended on
*every* `check_setup` call (not just fires), so there's always a real
`oi_alignment_lookback_bars`-bars-back comparison point by the time a
breakout is evaluated, not just whatever happened to exist in that instant.
`False` (default) is off, byte-identical to the base strategy. Sits out
(treated as a reject, not a pass) whenever there isn't yet a full lookback
window of history or PCR is unavailable — same "missing data isn't an
adverse regime, but also isn't a free pass" reasoning used everywhere else
in this codebase's gates, deliberately inverted here because an *absence*
of positioning evidence shouldn't count as confirmation.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from dataclasses import replace
from datetime import date

from sqlalchemy.orm import Session

from app.core.clock import to_ist
from app.domain.market.models import OptionContract, OptionType, PriceBar
from app.domain.strategy.models import StrategyRun
from app.modules.strategy_engine.conviction_gates import ConvictionGateMixin
from app.modules.strategy_engine.env_metrics import get_latest_env_metrics
from app.modules.strategy_engine.interface import TradeProposal
from app.modules.strategy_engine.strategies.oi_volume_confirmed import OIVolumeConfirmedStrategy

logger = logging.getLogger("app.strategy_engine.oi_volume_confirmed_conviction")


class OIVolumeConfirmedConvictionStrategy(ConvictionGateMixin, OIVolumeConfirmedStrategy):
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
        require_oi_price_alignment: bool = False,
        oi_alignment_lookback_bars: int = 5,
        **oi_kwargs: object,
    ) -> None:
        OIVolumeConfirmedStrategy.__init__(self, instrument_id, expiry_date, **oi_kwargs)  # type: ignore[arg-type]
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
        self.require_oi_price_alignment = require_oi_price_alignment
        self.oi_alignment_lookback_bars = oi_alignment_lookback_bars
        self._pcr_history: deque[tuple[int, float]] = deque(maxlen=oi_alignment_lookback_bars + 1)

    def _oi_alignment_reject(self, option_type: OptionType) -> bool:
        if len(self._pcr_history) < 2:
            return True
        oldest_bar, oldest_pcr = self._pcr_history[0]
        newest_bar, newest_pcr = self._pcr_history[-1]
        if newest_bar - oldest_bar < self.oi_alignment_lookback_bars:
            return True
        delta = newest_pcr - oldest_pcr
        if option_type is OptionType.CE:
            return delta >= 0  # PCR not falling -> no call-side buildup confirmation
        return delta <= 0  # PCR not rising -> no put-side buildup confirmation

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        proposal = OIVolumeConfirmedStrategy.check_setup(self, db, strategy_run, latest_bar)

        if self.require_oi_price_alignment:
            env = get_latest_env_metrics(db, self.instrument_id, self.expiry_date)
            pcr_now = env.get("pcr_oi") if env else None
            if pcr_now is not None:
                self._pcr_history.append((self.bar_count, float(pcr_now)))

        if proposal is None:
            return None

        contract = db.get(OptionContract, proposal.option_contract_id)
        if contract is None:
            return None
        # See vwap_pullback_conviction.py's identical comment: a freshly-
        # queried `OptionContract.option_type` can be a raw string (plain
        # `String(2)` column, no SQLAlchemy Enum type), which would silently
        # break every `is OptionType.CE`/`is OptionType.PE` identity check
        # in `ConvictionGateMixin` -- normalize once via the enum
        # constructor (idempotent for either input type). `_fired_directions
        # .discard()` below would actually still work on a raw string
        # (StrEnum hashes/compares equal to its own string value), but the
        # gate's own `is` checks would not -- normalize regardless, for one
        # consistent `option_type` value throughout this method.
        option_type = OptionType(contract.option_type)

        if self.require_oi_price_alignment and self._oi_alignment_reject(option_type):
            self._fired_directions.discard(option_type)
            self._log_once(
                logger,
                "conviction_oi_price_misaligned",
                "run %s: %s setup rejected -- PCR not confirming positioning direction",
                strategy_run.id,
                option_type.value,
            )
            self.last_signal_status.candidate = proposal
            return None

        bar_ist = to_ist(latest_bar.bucket_start)
        reject = self._conviction_reject_reason(db, latest_bar, option_type, bar_ist)
        if reject is not None:
            # Undo the base class's own latch -- see module docstring.
            self._fired_directions.discard(option_type)
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
            proposal, payload={**proposal.payload, "strategy": "oi_volume_confirmed_conviction"}
        )
