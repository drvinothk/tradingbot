"""Shared Strategy interface. A Strategy may only produce a `TradeProposal`
DTO and hand it to `strategy_engine.service.submit_signal` — it never
constructs a Signal/TradeIntent row itself and never imports anything under
`app.modules.execution_engine` or `app.domain.execution`, so "strategies
can't touch execution directly" is enforced by what's importable from this
module, not just by convention.

Phase 2's `strategies/synthetic.py` is the only implementation for now;
Phase 4 extends this with the common rules shared by the three
confirmation-filter strategies (full-candle completion, mandatory stop,
per-method trailing activation, spread/structure-break exit) — this
interface is deliberately minimal until then.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TypedDict

from sqlalchemy.orm import Session

from app.domain.market.models import PriceBar
from app.domain.strategy.exit_legs import ExitLegSpec
from app.domain.strategy.models import SignalSide, StrategyRun

__all__ = [
    "EnvPayload",
    "ExitLegSpec",
    "SignalStatus",
    "Strategy",
    "TradePayload",
    "TradeProposal",
]


class EnvPayload(TypedDict, total=False):
    """VIX/PCR environment metrics — always empty until the real data
    pipeline exists (`strategy_engine.env_metrics.get_latest_env_metrics`
    is a stub returning `None`). `total=False` since every key is
    independently optional, not a real runtime-validated shape (TypedDicts
    are mypy-only — this buys static typing, not validation).
    """

    vix: float | None
    pcr_oi: float | None
    pcr_vol: float | None


class TradePayload(TypedDict, total=False):
    """Union of every key any of the six strategies' payload dicts sets
    today, plus `env` for whenever env metrics are wired in. Deliberately
    one shared, all-optional shape rather than six near-duplicate
    per-strategy TypedDicts — payload is never runtime-validated either
    way, so per-strategy exactness wouldn't buy real safety, just more
    types to keep in sync.
    """

    strategy: str
    strike_score: float
    breakdown: dict[str, float]  # matches strike_ranking.engine.RankedContract.breakdown
    or_high: float
    or_low: float
    vwap: float
    ema9: float
    ema20: float
    window_high: float
    window_low: float
    env: EnvPayload | None


@dataclass(frozen=True)
class TradeProposal:
    option_contract_id: uuid.UUID
    side: SignalSide
    qty_lots: int
    entry_price: float
    stop_price: float
    target_price: float
    # Per-method trailing (Phase 4) — None means "use the generic Phase-3
    # 0.5/0.5 rule" (execution_engine.paper.service's TRAIL_ACTIVATION_
    # FRACTION/TRAIL_LOCK_FRACTION), which is what leaving these unset
    # preserves for SyntheticStrategy.
    trail_activation_fraction: float | None = None
    trail_lock_fraction: float | None = None
    # Underlying-index structural invalidation level (opening-range boundary
    # / pullback extreme / EMA9 value) — independent of stop_price/
    # target_price, which are on the option premium. None means no
    # structure-break exit is tracked for this position.
    structure_level: float | None = None
    # ATR-scaled minimum-breach margin (underlying index points) and minimum
    # persistence window (seconds) required before a structure_level breach
    # is confirmed rather than fired on a single noisy tick — see
    # execution_engine.paper.service.evaluate_open_position. None on either
    # means no buffer / confirm immediately (unbuffered instant-exit,
    # matching pre-fix behavior) — the default for any strategy that doesn't
    # set them.
    structure_break_buffer: float | None = None
    structure_break_persistence_seconds: float | None = None
    # Optional hard risk overlays, independent of stop_price/structure_level:
    #   max_loss_per_lot   — absolute INR loss per lot (entry_price - current) *
    #                        lot_size; exit the moment it is reached, even if the
    #                        premium stop_price hasn't been hit. Caps the fat
    #                        left tail (adverse-gap / structure-break slippage).
    #   time_stop_minutes  — minutes since entry after which the position is
    #                        closed if it is not in profit ("trades that work,
    #                        work fast"; a stale losing trade only bleeds theta).
    # Both None = no overlay, the default for any strategy that doesn't opt in.
    # As of 2026-08-28 these are consumed by the backtest exit reconstruction
    # only; production PositionManager wiring + the stop_plans columns are a
    # separate, deliberately-gated follow-up (see BACKTEST_TIME_CONVENTIONS.md
    # / the conviction-strategy plan).
    max_loss_per_lot: float | None = None
    time_stop_minutes: float | None = None
    # Multi-leg exit engine — an ordered list of sub-lot exit legs (see
    # `app.domain.strategy.exit_legs.ExitLegSpec`). `None` (the default, and
    # what every strategy emits until it opts in) means a single full-qty
    # exit driven by `stop_price`/`target_price`/`structure_level`/`trail_*`
    # above, byte-identical to today. When set, execution ignores those
    # scalar fields for stop/target/trail purposes and builds one
    # `PositionExitLeg` per spec instead. `qty_fraction` values must sum to
    # 1.0; validated by `validate_exit_leg_specs`.
    exit_legs: list[ExitLegSpec] | None = None
    payload: TradePayload = field(default_factory=lambda: TradePayload())


@dataclass
class SignalStatus:
    """Market Terminal's "signal panel" (2026-08-30): the most recent
    reason a `ConfirmationFilterStrategy` didn't fire this cycle, plus —
    when already resolved at that rejection point — the exact candidate
    that was rejected. Deliberately not part of the `Strategy`/`evaluate()`
    contract itself (no new abstract method, no signature change): every
    `ConfirmationFilterStrategy` instance just carries one of these as a
    plain mutable attribute (`self.last_signal_status`,
    `common_rules.ConfirmationFilterStrategy.__init__`), updated from
    inside `_log_once` and read externally via
    `strategy_engine.runner.StrategyRunner.last_signal_status`.
    `SyntheticStrategy` has no such attribute at all — callers use
    `getattr(strategy, "last_signal_status", None)`.

    `reason_code=None` means nothing has been rejected yet this run (still
    within the "just running, nothing to report" state the signal panel
    treats as plain Scanning). `candidate` is `None` whenever the
    rejection fired before a `TradeProposal` was resolved (e.g. an early
    "not enough bars yet" gate) — only conviction-gate-style rejections
    that run *after* a base strategy's own `check_setup` has already built
    one carry a candidate.
    """

    reason_code: str | None = None
    candidate: TradeProposal | None = None
    evaluated_at: datetime | None = None


class Strategy(ABC):
    # Every concrete strategy already stores these as plain instance
    # attributes (set in __init__, either directly or via
    # ConfirmationFilterStrategy's base __init__) — declared here formally
    # so `strategy_engine.runner.run_cycle` can read them generically for the
    # freshness gate without each strategy needing to expose anything new.
    instrument_id: uuid.UUID
    expiry_date: date

    @abstractmethod
    def evaluate(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar | None = None
    ) -> TradeProposal | None:
        """Called once per scan cycle. Returns a proposal to submit, or
        `None` if there's nothing to trade this cycle — "no signal" is a
        normal, frequent outcome, not an error.

        `latest_bar` is an optional pre-fetched bar from `strategy_engine
        .runner.run_cycle` (already fetched there for the trade-window
        gate) — a bar-consuming implementation (`ConfirmationFilterStrategy`)
        uses it instead of re-querying; `SyntheticStrategy`, which doesn't
        consume bars at all, ignores it.
        """
