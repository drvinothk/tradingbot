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

from sqlalchemy.orm import Session

from app.domain.strategy.models import SignalSide, StrategyRun


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
    payload: dict = field(default_factory=dict)


class Strategy(ABC):
    @abstractmethod
    def evaluate(self, db: Session, strategy_run: StrategyRun) -> TradeProposal | None:
        """Called once per scan cycle. Returns a proposal to submit, or
        `None` if there's nothing to trade this cycle — "no signal" is a
        normal, frequent outcome, not an error."""
