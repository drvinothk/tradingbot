"""Conviction-gated Opening Range Breakout.

A thin subclass of `ORBStrategy` that keeps ORB's exact opening-range /
breakout / strike-ranking logic and layers the "few high-conviction trades"
filters from the expert framework on top (HTF EMA-trend agreement, ATR /
volatility expansion, volume surge, India VIX regime band, prior-day-close
trend agreement, a hard trades-per-day cap, and an optional reward:risk
target override).

**Every gate is opt-in.** With all `require_*` flags left `False`, no
`vix_min`/`vix_max` set, `target_r_multiple=None`, and `max_trades_per_day`
at ORB's own natural ceiling of 2 (one entry per direction per run), this
class produces byte-identical proposals to plain `orb` — so a backtest of
`orb_conviction` with an empty `params` dict reproduces the `orb` baseline
exactly, and each gate's contribution can be measured by turning it on
alone.

**Backtest-relevant caveats:**

- The "HTF" EMA-trend gate reads `EMA9`/`EMA20` off the system-wide 60s
  bar (the only timeframe this codebase persists), not a true 15-min
  chart. It is a 9-minute-vs-20-minute-EMA proxy for the framework's
  15-min trend filter, not the literal thing.
- The volume-surge gate no-ops (does not block) whenever the breakout
  bar's own volume is 0 — index underlying feeds (`alice_index`) report
  zero volume, so this gate only bites when run against a real-volume
  source (`futures_proxy`).
- The VIX gate passes when no VIX tick is available as of the bar (missing
  data is not treated as an adverse regime).

Rolling back ORB's one-shot direction latch: `ORBStrategy` records a fired
direction in `self._fired_directions` and never re-fires it for the run.
When a conviction gate rejects an otherwise-valid breakout, this class
`discard()`s that direction again so a later bar can re-qualify once the
regime agrees — the breakout premise is still live, it just was not
high-conviction on that bar.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import date, datetime, time

from sqlalchemy.orm import Session

from app.core.clock import IST, to_ist
from app.domain.market.models import Instrument, OptionType, PriceBar
from app.domain.strategy.models import StrategyRun
from app.modules.strategy_engine.common_rules import (
    get_latest_indicator_value,
    get_recent_completed_bars,
    get_recent_indicator_values,
)
from app.modules.strategy_engine.env_metrics import get_vix_as_of
from app.modules.strategy_engine.interface import TradePayload, TradeProposal
from app.modules.strategy_engine.strategies.orb import ORBStrategy

logger = logging.getLogger("app.strategy_engine.orb_conviction")

# Extra tunables this subclass adds on top of every ORB param. Kept as an
# explicit literal (not derived) for the same reason the per-strategy
# *_PARAM_KEYS sets in `api.v1.strategies` are explicit literals: an
# unexpected key must fail loudly at construction, not leak through.
CONVICTION_PARAM_KEYS = {
    "require_htf_ema_trend",
    "htf_ema_slope_lookback",
    "require_atr_expansion",
    "atr_expansion_lookback",
    "atr_expansion_min_ratio",
    "require_volume_surge",
    "volume_surge_lookback",
    "volume_surge_min_ratio",
    "vix_min",
    "vix_max",
    "max_trades_per_day",
    "target_r_multiple",
    # 2026-08-28 batch — findings-driven gates + hard risk overlays
    "ce_only",
    "skip_weekdays",
    "min_breakout_strength_atr",
    "require_drift_alignment",
    "max_loss_per_lot",
    "time_stop_minutes",
    # 2026-08-29 — directional-regime gate (sweep #3 W1)
    "require_prior_day_trend",
    "prior_day_trend_buffer_pts",
}

# IST weekday names accepted by `skip_weekdays`.
_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


class ORBConvictionStrategy(ORBStrategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        *,
        require_htf_ema_trend: bool = False,
        htf_ema_slope_lookback: int = 5,
        require_atr_expansion: bool = False,
        atr_expansion_lookback: int = 20,
        atr_expansion_min_ratio: float = 1.0,
        require_volume_surge: bool = False,
        volume_surge_lookback: int = 20,
        volume_surge_min_ratio: float = 1.5,
        vix_min: float | None = None,
        vix_max: float | None = None,
        max_trades_per_day: int = 2,
        target_r_multiple: float | None = None,
        ce_only: bool = False,
        skip_weekdays: list[str] | None = None,
        min_breakout_strength_atr: float | None = None,
        require_drift_alignment: bool = False,
        require_prior_day_trend: bool = False,
        prior_day_trend_buffer_pts: float = 0.0,
        max_loss_per_lot: float | None = None,
        time_stop_minutes: float | None = None,
        **orb_kwargs: object,
    ) -> None:
        super().__init__(instrument_id, expiry_date, **orb_kwargs)  # type: ignore[arg-type]
        self.require_htf_ema_trend = require_htf_ema_trend
        self.htf_ema_slope_lookback = htf_ema_slope_lookback
        self.require_atr_expansion = require_atr_expansion
        self.atr_expansion_lookback = atr_expansion_lookback
        self.atr_expansion_min_ratio = atr_expansion_min_ratio
        self.require_volume_surge = require_volume_surge
        self.volume_surge_lookback = volume_surge_lookback
        self.volume_surge_min_ratio = volume_surge_min_ratio
        self.vix_min = vix_min
        self.vix_max = vix_max
        self.max_trades_per_day = max_trades_per_day
        self.target_r_multiple = target_r_multiple
        self.ce_only = ce_only
        self.skip_weekdays = {d for d in (skip_weekdays or []) if d in _WEEKDAYS}
        self.min_breakout_strength_atr = min_breakout_strength_atr
        self.require_drift_alignment = require_drift_alignment
        self.require_prior_day_trend = require_prior_day_trend
        self.prior_day_trend_buffer_pts = prior_day_trend_buffer_pts
        self.max_loss_per_lot = max_loss_per_lot
        self.time_stop_minutes = time_stop_minutes
        # Per-IST-day entry counter. Same in-memory durability class as
        # ORBStrategy._fired_directions — a process restart resets it, the
        # same way it resets the runner thread itself.
        self._entries_by_day: dict[date, int] = {}

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        proposal = super().check_setup(db, strategy_run, latest_bar)
        if proposal is None:
            return None

        bar_ist = to_ist(latest_bar.bucket_start)
        day = bar_ist.date()
        option_type = self._infer_direction(latest_bar, proposal)

        reject = self._conviction_reject_reason(db, latest_bar, proposal, option_type, day, bar_ist)
        if reject is not None:
            # Let a later bar re-qualify this direction — see module docstring.
            self._fired_directions.discard(option_type)
            self._log_once(
                logger,
                f"conviction_{reject}",
                "run %s: %s breakout rejected by conviction gate '%s'",
                strategy_run.id,
                option_type.value,
                reject,
            )
            return None

        self._entries_by_day[day] = self._entries_by_day.get(day, 0) + 1
        return self._finalize(db, proposal)

    # --- gates -----------------------------------------------------------

    def _conviction_reject_reason(
        self,
        db: Session,
        latest_bar: PriceBar,
        proposal: TradeProposal,
        option_type: OptionType,
        day: date,
        bar_ist: datetime,
    ) -> str | None:
        if self._entries_by_day.get(day, 0) >= self.max_trades_per_day:
            return "max_trades_per_day"

        if self.ce_only and option_type is not OptionType.CE:
            return "ce_only"

        if bar_ist.strftime("%A") in self.skip_weekdays:
            return "skip_weekday"

        if self.min_breakout_strength_atr is not None:
            reason = self._breakout_strength_reject(db, latest_bar, proposal, option_type)
            if reason is not None:
                return reason

        if self.require_drift_alignment:
            reason = self._drift_alignment_reject(db, latest_bar, option_type)
            if reason is not None:
                return reason

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
            return None  # index feed has no volume — this gate cannot apply
        prior = [float(b.volume or 0) for b in bars[:-1]]
        prior_avg = sum(prior) / len(prior)
        if prior_avg <= 0 or current < prior_avg * self.volume_surge_min_ratio:
            return "volume_not_surging"
        return None

    def _breakout_strength_reject(
        self, db: Session, latest_bar: PriceBar, proposal: TradeProposal, option_type: OptionType
    ) -> str | None:
        """|close - the OR boundary it broke| must be at least
        `min_breakout_strength_atr` * ATR14 — a marginal 1-2 pt poke past
        the range is not a conviction breakout."""
        assert self.min_breakout_strength_atr is not None  # caller-guarded
        atr = get_latest_indicator_value(db, self.instrument_id, "ATR14", self.timeframe)
        if atr is None or atr <= 0:
            return "breakout_strength_not_ready"
        or_high = proposal.payload.get("or_high")
        or_low = proposal.payload.get("or_low")
        if or_high is None or or_low is None:
            return "breakout_strength_not_ready"
        close = float(latest_bar.close)
        boundary = float(or_high) if option_type is OptionType.CE else float(or_low)
        if abs(close - boundary) < self.min_breakout_strength_atr * atr:
            return "breakout_too_weak"
        return None

    def _drift_alignment_reject(
        self, db: Session, latest_bar: PriceBar, option_type: OptionType
    ) -> str | None:
        """The breakout direction must agree with the underlying's net move
        since the 9:15 IST session open (don't buy a CE breakout on a day
        the index has been grinding down all morning)."""
        session_open = datetime.combine(
            to_ist(latest_bar.bucket_start).date(), time(9, 15), tzinfo=IST
        )
        bars = get_recent_completed_bars(
            db, self.instrument_id, self.timeframe, since=session_open,
            until=latest_bar.bucket_start,
        )
        if not bars:
            return "drift_not_ready"
        drift = float(latest_bar.close) - float(bars[0].open)
        if option_type is OptionType.CE and drift <= 0:
            return "drift_disagrees"
        if option_type is OptionType.PE and drift >= 0:
            return "drift_disagrees"
        return None

    def _prior_day_trend_reject(
        self, db: Session, latest_bar: PriceBar, option_type: OptionType
    ) -> str | None:
        """The breakout direction must agree with where the underlying now
        trades relative to the prior trading day's close: a CE (long)
        breakout needs price above prior close + buffer, a PE (short)
        breakout below prior close - buffer. This is the framework's daily
        trend filter (Strategy A = "ORB + trend"), using the day-over-day
        reference the 60s EMA9/EMA20 proxy cannot express.

        `prior_close` is the last completed 60s bar strictly before today's
        9:15 IST open — the prior session's close (or last available bar).
        """
        session_open = datetime.combine(
            to_ist(latest_bar.bucket_start).date(), time(9, 15), tzinfo=IST
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

    # --- helpers -------------------------------------------------------

    @staticmethod
    def _infer_direction(latest_bar: PriceBar, proposal: TradeProposal) -> OptionType:
        or_high = proposal.payload.get("or_high")
        close = float(latest_bar.close)
        if or_high is not None and close > float(or_high):
            return OptionType.CE
        return OptionType.PE

    def _finalize(self, db: Session, proposal: TradeProposal) -> TradeProposal:
        payload: TradePayload = {**proposal.payload, "strategy": "orb_conviction"}
        target_price = proposal.target_price
        if self.target_r_multiple is not None:
            risk = proposal.entry_price - proposal.stop_price
            if risk > 0:
                raw = proposal.entry_price + self.target_r_multiple * risk
                instrument = db.get(Instrument, self.instrument_id)
                tick = (
                    float(instrument.tick_size)
                    if instrument is not None and instrument.tick_size
                    else 0.05
                )
                target_price = round(raw / tick) * tick
        return replace(
            proposal,
            target_price=target_price,
            payload=payload,
            max_loss_per_lot=self.max_loss_per_lot,
            time_stop_minutes=self.time_stop_minutes,
        )
