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

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from app.domain.execution.models import ExitReason, Position, PositionStatus, TradeOutcome
from app.domain.market.models import OptionContract
from app.domain.session.models import TradingSession
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.execution_engine.paper.service import close_position, current_contract_price
from app.modules.market_data.providers.base import BaseMarketDataProvider


def _square_off_all_open_positions(
    db: Session,
    broker: BrokerPort,
    trading_session: TradingSession,
    exit_reason: ExitReason,
    *,
    market_data_provider: BaseMarketDataProvider | None = None,
) -> list[TradeOutcome]:
    open_positions = (
        db.query(Position)
        .filter(
            Position.trading_session_id == trading_session.id,
            Position.status == PositionStatus.OPEN,
        )
        .all()
    )

    @contextmanager
    def _same_session() -> Iterator[Session]:
        # Without this, current_contract_price's option-chain refresh would
        # default to a brand new, independently-committing session_scope()
        # -- a real, previously-documented trap in this codebase (a second
        # connection can't see this transaction's own uncommitted rows,
        # e.g. a test's own not-yet-committed Instrument/OptionContract).
        yield db

    outcomes: list[TradeOutcome] = []
    for position in open_positions:
        option_contract = db.get(OptionContract, position.option_contract_id)
        if option_contract is None:
            continue
        # Same REST-option-chain-snapshot-preferring price source as every
        # other paper-execution price decision -- was broker.get_quote()
        # directly, which (since get_execution_broker() always resolves to
        # the mock) returned the mock's own synthetic, strategy-independent
        # price, same as the bug current_contract_price exists to close.
        tick = current_contract_price(
            db,
            option_contract,
            broker,
            market_data_provider=market_data_provider,
            session_factory=_same_session,
        )
        outcome = close_position(
            db, trading_session, position, exit_reason, tick.ltp, broker=broker
        )
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def run_eod_square_off(
    db: Session,
    broker: BrokerPort,
    trading_session: TradingSession,
    *,
    market_data_provider: BaseMarketDataProvider | None = None,
) -> list[TradeOutcome]:
    return _square_off_all_open_positions(
        db,
        broker,
        trading_session,
        ExitReason.EOD_SQUARE_OFF,
        market_data_provider=market_data_provider,
    )


def run_margin_breach_square_off(
    db: Session,
    broker: BrokerPort,
    trading_session: TradingSession,
    *,
    market_data_provider: BaseMarketDataProvider | None = None,
) -> list[TradeOutcome]:
    return _square_off_all_open_positions(
        db,
        broker,
        trading_session,
        ExitReason.MARGIN_BREACH,
        market_data_provider=market_data_provider,
    )
