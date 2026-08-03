"""Forced square-off — closes every open position for a session regardless
of where price currently sits relative to its stop/target. Two callers share
`_square_off_all_open_positions`, differing only in `ExitReason`:

- `run_eod_square_off`: the mandatory end-of-day flatten, called by
  `PositionManager` once IST wall-clock passes `trading_session.cutoff_time`
  (see `app.core.clock.now_ist`).
- `run_margin_breach_square_off`: the Addendum hardening batch's one narrow
  automatic emergency-square-off trigger — a detected negative available
  margin on a guarded-live/live session (see `PositionManager._run_cycle`).
  Deliberately *not* triggered by connectivity loss, reconciliation lag, or
  anything else — kill-switch stays freeze-and-alert by design; this is a
  separate, additional control.

Both are safe to call repeatedly: a session with no open positions is a
no-op, and `close_position` itself no-ops on an already-closed position.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.execution.models import ExitReason, Position, PositionStatus, TradeOutcome
from app.domain.market.models import OptionContract
from app.domain.session.models import TradingSession
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.execution_engine.paper.service import close_position


def _square_off_all_open_positions(
    db: Session, broker: BrokerPort, trading_session: TradingSession, exit_reason: ExitReason
) -> list[TradeOutcome]:
    open_positions = (
        db.query(Position)
        .filter(
            Position.trading_session_id == trading_session.id,
            Position.status == PositionStatus.OPEN,
        )
        .all()
    )

    outcomes: list[TradeOutcome] = []
    for position in open_positions:
        option_contract = db.get(OptionContract, position.option_contract_id)
        if option_contract is None:
            continue
        tick = broker.get_quote(option_contract.symbol)
        outcome = close_position(
            db, trading_session, position, exit_reason, tick.ltp, broker=broker
        )
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def run_eod_square_off(
    db: Session, broker: BrokerPort, trading_session: TradingSession
) -> list[TradeOutcome]:
    return _square_off_all_open_positions(db, broker, trading_session, ExitReason.EOD_SQUARE_OFF)


def run_margin_breach_square_off(
    db: Session, broker: BrokerPort, trading_session: TradingSession
) -> list[TradeOutcome]:
    return _square_off_all_open_positions(db, broker, trading_session, ExitReason.MARGIN_BREACH)
