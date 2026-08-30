"""Cross-cutting entry-conviction gates, shared by every `*_conviction`
strategy subclass. Extracted 2026-08-30 from `orb_conviction.py` (the first,
and until now the only, strategy to use this pattern) once four more
strategies needed the identical gates -- a second, then third, copy-pasted
set of boolean flags was the wrong shape once more than one consumer showed
up (same reasoning `common_rules.py`'s own module docstring gives for why
*that* module exists).

Every gate is opt-in and no-ops when its `require_*`/`*_min`/`*_max` flag is
left at its default -- a strategy that mixes this in with every gate left off
produces byte-identical proposals to its own base strategy (the same
guarantee `orb_conviction.py` originally promised, preserved here and now
covered by `orb_conviction.py`'s own unchanged test suite post-refactor).

Unlike `orb_conviction.py`'s own gates (which had to infer CE/PE from
`or_high`/`latest_bar.close` since ORB's `TradeProposal` doesn't carry
direction explicitly), every subclass using this mixin already knows its own
direction before calling `_conviction_reject_reason` -- callers resolve it
from the finalized `TradeProposal.option_contract_id` via a DB lookup (see
e.g. `vwap_pullback_conviction.py`), not re-derived from bar data.

Deliberately NOT ported here: `require_drift_alignment` (confirmed, sweep #3
2026-08-29, to reject nothing on real data -- a dead lever, see project
memory `project_orb_directional_filter_sweep3_2026_08_29`) and
`min_breakout_strength_atr`/`target_r_multiple` (ORB-specific, tied to
`or_high`/`or_low` -- no clean equivalent across all four target strategies).

**PCR gate** (`pcr_oi_min`/`pcr_oi_max`): new, not ported from anywhere --
`env_metrics.compute_pcr` already computes a put/call OI ratio for every
strategy's signal payload, but nothing has ever gated an entry on it.
`run_backtest.py` force-refreshes the option-chain snapshot every single bar
(see that script's own "option-chain freshness gate" docstring section), so
PCR is genuinely time-varying through a backtest day in this harness, not a
single static per-run value the way `oi_use_atm_oi_buildup`'s temporal-
OI-buildup stub is limited to in production. Direction-agnostic band (same
shape as the VIX gate) for this first pass -- deliberately not committing to
a CE/PE-specific polarity before a sweep shows which band position (if any)
actually helps.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from datetime import time as dtime

from sqlalchemy.orm import Session

from app.core.clock import IST, to_ist
from app.domain.market.models import OptionType, PriceBar
from app.modules.strategy_engine.common_rules import (
    get_latest_indicator_value,
    get_recent_completed_bars,
    get_recent_indicator_values,
)
from app.modules.strategy_engine.env_metrics import get_latest_env_metrics, get_vix_as_of

# Every param key this mixin's __init__ accepts -- union'd into each concrete
# `*_conviction` subclass's own PARAM_KEYS constant in `api.v1.strategies`,
# same pattern `orb_conviction.CONVICTION_PARAM_KEYS` already established for
# ORB (kept as an explicit literal here, not re-derived, so the two can never
# drift -- identical reasoning to that module's own comment).
CONVICTION_GATE_PARAM_KEYS = {
    "require_prior_day_trend",
    "prior_day_trend_buffer_pts",
    "vix_min",
    "vix_max",
    "require_atr_expansion",
    "atr_expansion_lookback",
    "atr_expansion_min_ratio",
    "require_volume_surge",
    "volume_surge_lookback",
    "volume_surge_min_ratio",
    "require_htf_ema_trend",
    "htf_ema_slope_lookback",
    "pcr_oi_min",
    "pcr_oi_max",
    "skip_weekdays",
}

# IST weekday names accepted by `skip_weekdays` -- identical set
# `orb_conviction.py` already validates against.
_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


class ConvictionGateMixin:
    """Mixed in ahead of a `ConfirmationFilterStrategy` subclass (see each
    `*_conviction.py` file for the exact MRO, e.g.
    `class VWAPPullbackConvictionStrategy(ConvictionGateMixin, VWAPPullbackStrategy)`).
    This class owns no `instrument_id`/`timeframe`/`expiry_date` of its own --
    it reads them off `self` at call time, relying on the concrete
    subclass's own `__init__` (via its base strategy's
    `ConfirmationFilterStrategy.__init__`) having already set them before any
    gate method runs. Concrete subclasses must call
    `ConvictionGateMixin.__init__(self, ...)` explicitly (this is not a
    cooperative `super().__init__()` chain member), then call
    `self._conviction_reject_reason(...)` from their own `check_setup` once
    they have a finalized `TradeProposal` and know its direction.
    """

    # Declared, not assigned -- satisfied by the concrete subclass's own MRO
    # (its base strategy's `ConfirmationFilterStrategy.__init__` sets
    # `instrument_id`/`timeframe`; the strategy itself sets `expiry_date`).
    # Standard mypy mixin pattern: tells the type checker these attributes
    # exist without this class owning or initializing them.
    instrument_id: uuid.UUID
    timeframe: str
    expiry_date: date

    def __init__(
        self,
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
    ) -> None:
        self.require_prior_day_trend = require_prior_day_trend
        self.prior_day_trend_buffer_pts = prior_day_trend_buffer_pts
        self.vix_min = vix_min
        self.vix_max = vix_max
        self.require_atr_expansion = require_atr_expansion
        self.atr_expansion_lookback = atr_expansion_lookback
        self.atr_expansion_min_ratio = atr_expansion_min_ratio
        self.require_volume_surge = require_volume_surge
        self.volume_surge_lookback = volume_surge_lookback
        self.volume_surge_min_ratio = volume_surge_min_ratio
        self.require_htf_ema_trend = require_htf_ema_trend
        self.htf_ema_slope_lookback = htf_ema_slope_lookback
        self.pcr_oi_min = pcr_oi_min
        self.pcr_oi_max = pcr_oi_max
        self.skip_weekdays = {d for d in (skip_weekdays or []) if d in _WEEKDAYS}

    def _conviction_reject_reason(
        self,
        db: Session,
        latest_bar: PriceBar,
        option_type: OptionType,
        bar_ist: datetime,
    ) -> str | None:
        """Returns the first gate's rejection reason (opt-in gates checked in
        a fixed order, cheapest/no-DB-query gates first), or `None` if every
        enabled gate passes."""
        if bar_ist.strftime("%A") in self.skip_weekdays:
            return "skip_weekday"

        if self.require_prior_day_trend:
            reason = self._prior_day_trend_reject(db, latest_bar, option_type)
            if reason is not None:
                return reason

        if self.vix_min is not None or self.vix_max is not None:
            vix = get_vix_as_of(db, latest_bar.bucket_start)
            if vix is not None:
                if self.vix_min is not None and vix < self.vix_min:
                    return "vix_below_band"
                if self.vix_max is not None and vix > self.vix_max:
                    return "vix_above_band"

        if self.pcr_oi_min is not None or self.pcr_oi_max is not None:
            reason = self._pcr_reject(db)
            if reason is not None:
                return reason

        if self.require_htf_ema_trend:
            reason = self._htf_ema_reject(db, option_type)
            if reason is not None:
                return reason

        if self.require_atr_expansion:
            reason = self._atr_expansion_reject(db)
            if reason is not None:
                return reason

        if self.require_volume_surge:
            reason = self._volume_surge_reject(db, latest_bar)
            if reason is not None:
                return reason

        return None

    # --- gates -------------------------------------------------------

    def _prior_day_trend_reject(
        self, db: Session, latest_bar: PriceBar, option_type: OptionType
    ) -> str | None:
        """Ported verbatim (shape unchanged) from
        `orb_conviction.ORBConvictionStrategy._prior_day_trend_reject` -- see
        that method's own docstring for the full "why"."""
        session_open = datetime.combine(
            to_ist(latest_bar.bucket_start).date(), dtime(9, 15), tzinfo=IST
        )
        prior = get_recent_completed_bars(
            db, self.instrument_id, self.timeframe, until=session_open, limit=1
        )
        if not prior:
            return "prior_day_not_ready"
        prior_close = float(prior[0].close)
        close_now = float(latest_bar.close)
        buf = self.prior_day_trend_buffer_pts
        if option_type is OptionType.CE and close_now <= prior_close + buf:
            return "prior_day_trend_disagrees"
        if option_type is OptionType.PE and close_now >= prior_close - buf:
            return "prior_day_trend_disagrees"
        return None

    def _pcr_reject(self, db: Session) -> str | None:
        env = get_latest_env_metrics(db, self.instrument_id, self.expiry_date)
        if env is None:
            return None  # no chain snapshot yet -- missing data isn't an adverse regime
        pcr_oi = env.get("pcr_oi")
        if pcr_oi is None:
            return None
        if self.pcr_oi_min is not None and pcr_oi < self.pcr_oi_min:
            return "pcr_below_band"
        if self.pcr_oi_max is not None and pcr_oi > self.pcr_oi_max:
            return "pcr_above_band"
        return None

    def _htf_ema_reject(self, db: Session, option_type: OptionType) -> str | None:
        need = self.htf_ema_slope_lookback + 1
        ema9 = get_recent_indicator_values(
            db, self.instrument_id, "EMA9", self.timeframe, limit=need
        )
        ema20 = get_latest_indicator_value(db, self.instrument_id, "EMA20", self.timeframe)
        if len(ema9) < need or ema20 is None:
            return "htf_ema_not_ready"
        slope = ema9[-1] - ema9[0]
        if option_type is OptionType.CE:
            if not (ema9[-1] > ema20 and slope > 0):
                return "htf_ema_trend_disagrees"
        else:
            if not (ema9[-1] < ema20 and slope < 0):
                return "htf_ema_trend_disagrees"
        return None

    def _atr_expansion_reject(self, db: Session) -> str | None:
        need = self.atr_expansion_lookback + 1
        atrs = get_recent_indicator_values(
            db, self.instrument_id, "ATR14", self.timeframe, limit=need
        )
        if len(atrs) < need:
            return "atr_not_ready"
        prior_avg = sum(atrs[:-1]) / len(atrs[:-1])
        if prior_avg <= 0 or atrs[-1] <= prior_avg * self.atr_expansion_min_ratio:
            return "atr_not_expanding"
        return None

    def _volume_surge_reject(self, db: Session, latest_bar: PriceBar) -> str | None:
        need = self.volume_surge_lookback + 1
        bars = get_recent_completed_bars(db, self.instrument_id, self.timeframe, limit=need)
        if len(bars) < need:
            return "volume_not_ready"
        current = float(bars[-1].volume or 0)
        if current <= 0:
            return None  # index feed has no volume -- this gate cannot apply
        prior = [float(b.volume or 0) for b in bars[:-1]]
        prior_avg = sum(prior) / len(prior)
        if prior_avg <= 0 or current < prior_avg * self.volume_surge_min_ratio:
            return "volume_not_surging"
        return None
