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
from app.domain.execution.models import (
    ExitReason,
    Order,
    OrderMode,
    Position,
    PositionStatus,
    TradeOutcome,
)
from app.domain.market.models import OptionContract
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import TradingSession
from app.modules.alerting.manager import send_alert
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.composition import is_execution_broker_live
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


class NoUsableSquareOffPriceError(Exception):
    """Raised by `run_single_position_square_off` when `current_contract_price`
    can resolve no usable price anywhere (`PriceRead.none()` — a total,
    sustained option-chain blackout for this contract; Shoonya spot-leak with
    no fresh feed and no plausible snapshot, even stale,
    docs/ops/shoonya_option_chain_spot_leak.md).

    We deliberately do NOT fabricate a price to force a close: `close_position`
    derives its LIVE fire-now SL-LMT trigger from `intended_price`, so a `0.0`
    there sets a trigger that never fires (a silent non-exit), and for a paper
    position a spot-shaped number would book a fabricated P&L. Both are worse
    than an honest "left OPEN + loud". Real risk is not stranded — a
    broker-side resting SL-LMT stays live and the exchange force-squares-off
    every intraday option position at ~15:15-15:20 IST regardless.

    The batch sweep (`_square_off_all_open_positions`) catches this, logs, and
    keeps flattening the rest; `api.v1.execution.square_off_position` surfaces
    it as `success: false` with a distinct `reason`. The `option_chain_
    degraded` alert raised alongside is `mode=LIVE` for a live-broker position
    (reaches Telegram) / `mode=PAPER` otherwise (DB / Control Room only).
    """

    def __init__(self, position_id: uuid.UUID) -> None:
        self.position_id = position_id
        super().__init__(f"no usable square-off price for position {position_id}")


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

    # Same provenance-gated price source (`PriceRead`) as PositionManager's
    # stop/target/trail path.
    price = current_contract_price(
        db,
        option_contract,
        position_broker,
        market_data_provider=market_data_provider,
        session_factory=same_session,
    )
    if price.tick is None:
        # No usable price anywhere -- not actionable, not even a stale
        # snapshot (a total, sustained option-chain blackout for this
        # contract). We deliberately do NOT fabricate a price to force a
        # close: `close_position`'s LIVE fire-now path derives the SL-LMT
        # trigger from `intended_price`, so a `0.0` there would set a trigger
        # that never fires -- a silent non-exit, worse than an honest "left
        # OPEN + loud". Real risk is not stranded: any broker-side resting
        # SL-LMT stays live, and the exchange force-squares-off every
        # intraday option position at ~15:15-15:20 IST regardless. Leave it
        # OPEN, alert, and let the next PositionManager cycle / the operator
        # act once a price returns.
        is_live = is_execution_broker_live(position_broker)
        logger.error(
            "square-off of %s position %s: no usable price anywhere -- leaving it OPEN "
            "and raising NoUsableSquareOffPriceError (broker resting stop + exchange "
            "intraday square-off remain the backstops)",
            "LIVE" if is_live else "PAPER",
            position.id,
        )
        send_alert(
            db,
            workspace_id=trading_session.workspace_id,
            trading_session_id=trading_session.id,
            severity=AlertSeverity.CRITICAL,
            category="option_chain_degraded",
            message=(
                f"Forced square-off of {'LIVE' if is_live else 'paper'} position "
                f"{position.id} could not resolve any usable option price "
                f"({option_contract.symbol}); the position is left OPEN. Check the "
                "market-data feed / option chain; square off in the broker app if LIVE."
            )[:500],
            # LIVE -> reaches Telegram (CRITICAL + non-paper mode passes the
            # paper-suppression gate); paper -> DB/Control-Room only.
            mode=OrderMode.LIVE if is_live else OrderMode.PAPER,
            dedup_key=f"no_squareoff_price:{position.id}",
        )
        raise NoUsableSquareOffPriceError(position.id)

    exit_ltp = price.tick.ltp
    if not price.is_actionable:
        logger.warning(
            "square-off of position %s pricing from a %s/%s read (ltp=%.2f) -- "
            "reference price / slippage may be stale",
            position.id,
            price.source.value,
            price.freshness.value,
            exit_ltp,
        )

    return close_position(
        db, trading_session, position, exit_reason, exit_ltp, broker=position_broker, force=True
    )


def _square_off_all_open_positions(
    db: Session,
    broker: BrokerPort | None,
    trading_session: TradingSession,
    exit_reason: ExitReason,
    *,
    market_data_provider: BaseMarketDataProvider | None = None,
    live_only: bool = False,
) -> list[TradeOutcome]:
    query = db.query(Position).filter(
        Position.trading_session_id == trading_session.id,
        Position.status == PositionStatus.OPEN,
    )
    if live_only:
        # Only positions whose *opening* order was LIVE -- never inferred from
        # the current session mode. Same pattern api.v1.system_settings's
        # open-live-position check uses. Multi-leg positions are paper-only in
        # code (build_position_exit_legs returns None for LIVE), so this
        # correctly leaves every open paper position -- staged or not -- alone.
        query = query.join(Order, Order.id == Position.opening_order_id).filter(
            Order.mode == OrderMode.LIVE
        )
    open_positions = query.all()

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
        except NoUsableSquareOffPriceError:
            # No resolvable price for this contract (spot-leak blackout).
            # Already logged + alerted in run_single_position_square_off;
            # leave it OPEN and keep flattening the rest. Self-heals on the
            # next PositionManager cycle once a real price returns; a LIVE
            # position is also backstopped by its resting stop + the
            # exchange's own intraday square-off.
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


def run_kill_switch_square_off(
    db: Session,
    broker: BrokerPort | None,
    trading_session: TradingSession,
    *,
    market_data_provider: BaseMarketDataProvider | None = None,
) -> list[TradeOutcome]:
    """The operator's manual Kill Switch flatten -- LIVE positions only. Open
    paper positions keep being monitored (stop/target/trail) after the
    session drops to paper_only, per the 2026-09-09 redesign.
    """
    return _square_off_all_open_positions(
        db,
        broker,
        trading_session,
        ExitReason.KILL_SWITCH,
        market_data_provider=market_data_provider,
        live_only=True,
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
