"""EOD forced square-off — the mandatory end-of-day flatten, regardless of
where price currently sits relative to a position's stop/target. Called by
`PositionManager` once IST wall-clock passes `trading_session.cutoff_time`
(see `app.core.clock.now_ist`), and safe to call repeatedly: a session with
no open positions is a no-op, and `close_position` itself no-ops on an
already-closed position.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.execution.models import ExitReason, Position, PositionStatus, TradeOutcome
from app.domain.market.models import OptionContract
from app.domain.session.models import TradingSession
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.execution_engine.paper.service import close_position


def run_eod_square_off(
    db: Session, broker: BrokerPort, trading_session: TradingSession
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
            db, trading_session, position, ExitReason.EOD_SQUARE_OFF, tick.ltp, broker=broker
        )
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes
