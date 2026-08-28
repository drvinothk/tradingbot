"""Forced square-off — closes every open position for a session regardless
of where price currently sits relative to its stop/target. Two callers share
`_square_off_all_open_positions`, differing only in `ExitReason`:

- `run_eod_square_off`: the mandatory end-of-day flatten, called by
  `PositionManager` once IST wall-clock passes `trading_session.cutoff_time`
  (see `app.core.clock.now_ist`).
- `run_margin_breach_square_off`: the Addendum hardening batch's one narrow
  automatic emergency-square-off trigger — a detected negative available
  margin on a live session (see `PositionManager._run_cycle`).
  Deliberately *not* triggered by connectivity loss, reconciliation lag, or
  anything else — kill-switch stays freeze-and-alert by design; this is a
  separate, additional control.

Both are safe to call repeatedly: a session with no open positions is a
no-op, and `close_position` itself no-ops on an already-closed position.

**`broker` is optional, resolved per-position when omitted (2026-08-19
fix)**: a single caller-supplied broker used to be applied to *every* open
position being force-closed, regardless of which strategy opened each one —
the same bug shape `execution_engine.paper.service
.resolve_broker_for_position`'s own docstring describes for
`PositionManager._run_cycle`'s stop/target/trail path (a `force_paper`
strategy's position force-closed via the real broker the instant a session
reached `live_enabled`). Pass an explicit `broker` only to override every
position uniformly — the established test-fake pattern, unchanged; leave it
`None` in production so each position resolves its own correct broker.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.core.db.session import reuse_session
from app.domain.execution.models import ExitReason, Position, PositionStatus, TradeOutcome
from app.domain.market.models import OptionContract
from app.domain.session.models import TradingSession
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.execution_engine.paper.service import (
    close_position,
    current_contract_price,
    resolve_broker_for_position,
)
from app.modules.market_data.providers.base import BaseMarketDataProvider

logger = logging.getLogger("app.scheduler.eod_square_off")


class UnresolvableOptionContractError(Exception):
    """Raised by `run_single_position_square_off` when a `Position`'s own
    `option_contract_id` doesn't resolve to a real `OptionContract` row -- a
    data-integrity problem, categorically different from "the exit order
    didn't fill synchronously" (a normal, expected timing outcome that keeps
    returning `None`, same as before). Callers that need to tell the two
    apart -- `api.v1.execution.square_off_position`, so it can give the user
    an accurate message instead of a misleading "wait for reconciliation/
    retry" -- can catch this distinctly. The EOD/margin-breach batch sweep
    (`_square_off_all_open_positions`) deliberately catches this and moves
    on to the next position instead, since one corrupt position must never
    block the rest of a session's forced flatten.
    """

    def __init__(self, option_contract_id: uuid.UUID) -> None:
        self.option_contract_id = option_contract_id
        super().__init__(f"unknown option_contract_id {option_contract_id}")


def run_single_position_square_off(
    db: Session,
    broker: BrokerPort | None,
    trading_session: TradingSession,
    position: Position,
    exit_reason: ExitReason,
    *,
    market_data_provider: BaseMarketDataProvider | None = None,
) -> TradeOutcome | None:
    """The resolve-broker -> price -> `close_position` chain
    `_square_off_all_open_positions` runs per-position, factored out so a
    single-position manual square-off
    (`POST /positions/{id}/square-off`, `api.v1.execution.square_off_position`)
    can reuse it exactly instead of duplicating the resolution chain.
    `broker` follows the same convention as the batch callers below: `None`
    resolves the correct broker for *this* position's own strategy via
    `resolve_broker_for_position` (never a single broker shared across
    positions -- see this module's own docstring), a caller-supplied broker
    overrides it (the established test-fake pattern).

    Returns `None` if the position is already closed or the exit order
    didn't fill synchronously (see `close_position`'s own docstring) -- a
    `None` outcome in either of those cases is not itself an error, same as
    every other caller of `close_position` in this codebase already treats
    it. Raises `UnresolvableOptionContractError` if the option contract
    can't be resolved -- a data-integrity problem, not a timing one, so it
    must not be collapsed into the same `None` return as the two genuinely
    unremarkable cases above (see that exception's own docstring).
    """
    position_broker = broker or resolve_broker_for_position(db, trading_session, position)
    option_contract = db.get(OptionContract, position.option_contract_id)
    if option_contract is None:
        raise UnresolvableOptionContractError(position.option_contract_id)

    # reuse_session: without this, current_contract_price's option-chain
    # refresh would default to a brand new, independently-committing
    # session_scope() -- a real, previously-documented trap in this
    # codebase (a second connection can't see this transaction's own
    # uncommitted rows, e.g. a test's own not-yet-committed
    # Instrument/OptionContract).
    same_session = reuse_session(db)

    # Same REST-option-chain-snapshot-preferring price source as every
    # other paper-execution price decision -- was broker.get_quote()
    # directly, which (since get_execution_broker() always resolves to
    # the mock) returned the mock's own synthetic, strategy-independent
    # price, same as the bug current_contract_price exists to close.
    tick = current_contract_price(
        db,
        option_contract,
        position_broker,
        market_data_provider=market_data_provider,
        session_factory=same_session,
    )
    return close_position(
        db, trading_session, position, exit_reason, tick.ltp, broker=position_broker
    )


def _square_off_all_open_positions(
    db: Session,
    broker: BrokerPort | None,
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

    outcomes: list[TradeOutcome] = []
    for position in open_positions:
        try:
            outcome = run_single_position_square_off(
                db,
                broker,
                trading_session,
                position,
                exit_reason,
                market_data_provider=market_data_provider,
            )
        except UnresolvableOptionContractError:
            # A data-integrity problem on one position must never block the
            # rest of a session's forced flatten -- log and move on to the
            # next position, same effective behavior this batch sweep
            # already had before UnresolvableOptionContractError existed
            # (when this case was silently folded into a `None` return).
            logger.error(
                "square-off skipped position %s: option_contract_id %s does not "
                "resolve to a real OptionContract row",
                position.id,
                position.option_contract_id,
            )
            continue
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def run_eod_square_off(
    db: Session,
    broker: BrokerPort | None,
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
    broker: BrokerPort | None,
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
