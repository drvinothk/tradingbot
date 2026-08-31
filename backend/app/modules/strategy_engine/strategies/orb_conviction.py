"""Conviction-gated Opening Range Breakout.

A thin subclass of `ORBStrategy` that keeps ORB's exact opening-range /
breakout / strike-ranking logic and layers the "few high-conviction trades"
filters from the expert framework on top (HTF EMA-trend agreement, ATR /
volatility expansion, volume surge, India VIX regime band, PCR band,
prior-day-close trend agreement, a hard trades-per-day cap, and an optional
reward:risk target override).

**Every gate is opt-in.** With all `require_*` flags left `False`, no
`vix_min`/`vix_max`/`pcr_oi_min`/`pcr_oi_max` set, `target_r_multiple=None`,
and `max_trades_per_day` at ORB's own natural ceiling of 2 (one entry per
direction per run), this class produces byte-identical proposals to plain
`orb` — so a backtest of `orb_conviction` with an empty `params` dict
reproduces the `orb` baseline exactly, and each gate's contribution can be
measured by turning it on alone.

**2026-08-30 refactor**: the cross-cutting gates (prior-day trend, VIX band,
ATR expansion, volume surge, HTF EMA trend, the new PCR band, skip_weekdays)
moved to `conviction_gates.ConvictionGateMixin` once four more strategies
needed the identical gates — this class now mixes that in
(`ORBConvictionStrategy(ConvictionGateMixin, ORBStrategy)`) rather than
owning its own copy, so there's one source of truth for these gates going
forward. Only ORB-specific gates stay here: `max_trades_per_day`, `ce_only`,
`min_breakout_strength_atr`, `require_drift_alignment` (kept — already
shipped/tested even though sweep #3 found it rejects nothing on real data —
not being ported to the new strategies, but not removed from ORB either),
plus the exit-shaping `target_r_multiple`/`max_loss_per_lot`/
`time_stop_minutes`. Behavior for every existing gate is unchanged — this is
a pure extraction, covered by this class's own pre-existing test suite.

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
  data is not treated as an adverse regime); same convention for the PCR gate.

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
)
from app.modules.strategy_engine.conviction_gates import (
    CONVICTION_GATE_PARAM_KEYS,
    ConvictionGateMixin,
)
from app.modules.strategy_engine.interface import TradePayload, TradeProposal
from app.modules.strategy_engine.strategies.orb import ORBStrategy

logger = logging.getLogger("app.strategy_engine.orb_conviction")

# ORB-only tunables (gates + exit-shaping) this subclass adds on top of every
# ORB param and every shared `ConvictionGateMixin` gate. Kept as an explicit
# literal (not derived) for the same reason the per-strategy *_PARAM_KEYS
# sets in `api.v1.strategies` are explicit literals: an unexpected key must
# fail loudly at construction, not leak through.
ORB_ONLY_CONVICTION_PARAM_KEYS = {
    "max_trades_per_day",
    "target_r_multiple",
    "ce_only",
    "min_breakout_strength_atr",
    "require_drift_alignment",
    "max_loss_per_lot",
    "time_stop_minutes",
}
CONVICTION_PARAM_KEYS = ORB_ONLY_CONVICTION_PARAM_KEYS | CONVICTION_GATE_PARAM_KEYS


class ORBConvictionStrategy(ConvictionGateMixin, ORBStrategy):
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
        pcr_oi_min: float | None = None,
        pcr_oi_max: float | None = None,
        require_prior_day_trend: bool = False,
        prior_day_trend_buffer_pts: float = 0.0,
        skip_weekdays: list[str] | None = None,
        max_trades_per_day: int = 2,
        target_r_multiple: float | None = None,
        ce_only: bool = False,
        min_breakout_strength_atr: float | None = None,
        require_drift_alignment: bool = False,
        max_loss_per_lot: float | None = None,
        time_stop_minutes: float | None = None,
        require_rsi_alignment: bool = False,
        rsi_neutral_band: float = 10.0,
        **orb_kwargs: object,
    ) -> None:
        ORBStrategy.__init__(self, instrument_id, expiry_date, **orb_kwargs)  # type: ignore[arg-type]
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
        )
        self.max_trades_per_day = max_trades_per_day
        self.target_r_multiple = target_r_multiple
        self.ce_only = ce_only
        self.min_breakout_strength_atr = min_breakout_strength_atr
        self.require_drift_alignment = require_drift_alignment
        self.max_loss_per_lot = max_loss_per_lot
        self.time_stop_minutes = time_stop_minutes
        # Per-IST-day entry counter. Same in-memory durability class as
        # ORBStrategy._fired_directions — a process restart resets it, the
        # same way it resets the runner thread itself.
        self._entries_by_day: dict[date, int] = {}

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        proposal = ORBStrategy.check_setup(self, db, strategy_run, latest_bar)
        if proposal is None:
            return None

        bar_ist = to_ist(latest_bar.bucket_start)
        day = bar_ist.date()
        option_type = self._infer_direction(latest_bar, proposal)

        reject = self._orb_only_reject_reason(db, latest_bar, proposal, option_type, day)
        if reject is None:
            reject = self._conviction_reject_reason(db, latest_bar, option_type, bar_ist)
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
            self.last_signal_status.candidate = proposal
            return None

        self._entries_by_day[day] = self._entries_by_day.get(day, 0) + 1
        return self._finalize(db, proposal)

    # --- ORB-only gates ---------------------------------------------------

    def _orb_only_reject_reason(
        self,
        db: Session,
        latest_bar: PriceBar,
        proposal: TradeProposal,
        option_type: OptionType,
        day: date,
    ) -> str | None:
        if self._entries_by_day.get(day, 0) >= self.max_trades_per_day:
            return "max_trades_per_day"

        if self.ce_only and option_type is not OptionType.CE:
            return "ce_only"

        if self.min_breakout_strength_atr is not None:
            reason = self._breakout_strength_reject(db, latest_bar, proposal, option_type)
            if reason is not None:
                return reason

        if self.require_drift_alignment:
            reason = self._drift_alignment_reject(db, latest_bar, option_type)
            if reason is not None:
                return reason

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
