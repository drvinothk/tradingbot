"""Paper Execution Service — dispatch, exit (stop/target/trail), and
idempotency. Requires a real Postgres (LOCK_EXECUTION_SINGLETON/
LOCK_RISK_EVALUATION_QUEUE advisory locks aren't meaningfully testable
against SQLite, same reasoning as test_risk_engine.py). Each test builds its
own `MockBrokerAdapter()` and passes it explicitly via `broker=` rather than
relying on the process-wide `get_broker()` singleton, so fill prices are
fully under the test's control instead of depending on global state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.domain.execution.models import (
    ExitReason,
    Order,
    OrderMode,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    StopPlan,
    TradeOutcome,
    TrailPlan,
    TrailPlanStatus,
)
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.ops.models import SystemAlert
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import (
    ExecutionMode,
    Signal,
    SignalSide,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    StrategyStatus,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.broker_adapter import composition
from app.modules.broker_adapter.base.contracts import BrokerOrderStatus, OrderResult
from app.modules.broker_adapter.base.errors import BrokerError
from app.modules.broker_adapter.mock.adapter import FillScenario, MockBrokerAdapter
from app.modules.execution_engine.paper.service import (
    close_position,
    dispatch_trade_intent,
    evaluate_open_position,
    reconcile_pending_live_exit_orders,
    reconcile_pending_live_orders,
)
from app.modules.scheduler.eod_square_off import (
    UnresolvableOptionContractError,
    run_eod_square_off,
    run_single_position_square_off,
)

EXPIRY = date(2026, 7, 30)


@pytest.fixture
def broker() -> MockBrokerAdapter:
    return MockBrokerAdapter()


def _price(value: float | None) -> float:
    assert value is not None
    return float(value)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="exec-test-account",
        credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    return account


@pytest.fixture
def trading_session(db: Session, workspace, broker_account, user: User) -> TradingSession:
    ts = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=SafeMode.PAPER_ONLY,
        started_at=datetime.now(UTC),
        budget_amount=1_000_000,
        daily_target_profit=1_000_000,
        daily_loss_cap=1_000_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(ts)
    db.flush()
    return ts


@pytest.fixture
def instrument(db: Session) -> Instrument:
    inst = Instrument(id=uuid.uuid4(), symbol="NIFTY", exchange="NFO", lot_size=25, tick_size=0.05)
    db.add(inst)
    db.flush()
    return inst


@pytest.fixture
def option_contract(db: Session, instrument: Instrument) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
        strike=22000,
        option_type=OptionType.CE,
        symbol="NIFTY26JUL22000CE-EXEC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="exec-test-strategy")
    db.add(config)
    db.flush()
    return config


@pytest.fixture
def strategy_run(
    db: Session, strategy_config: StrategyConfig, trading_session, user: User
) -> StrategyRun:
    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(run)
    db.flush()
    return run


def _make_trade_intent(
    db: Session,
    trading_session: TradingSession,
    strategy_run: StrategyRun,
    option_contract: OptionContract,
    *,
    side: SignalSide = SignalSide.BUY,
    entry_price: float = 80.0,
    stop_price: float = 72.0,
    target_price: float = 92.0,
    qty_lots: int = 1,
    trail_activation_fraction: float | None = None,
    trail_lock_fraction: float | None = None,
    structure_level: float | None = None,
) -> TradeIntent:
    now = datetime.now(UTC)
    signal = Signal(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        strategy_config_id=strategy_run.strategy_config_id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        qty_lots=qty_lots,
        trail_activation_fraction=trail_activation_fraction,
        trail_lock_fraction=trail_lock_fraction,
        structure_level=structure_level,
        generated_at=now,
    )
    db.add(signal)
    db.flush()

    trade_intent = TradeIntent(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        signal_id=signal.id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        idempotency_key=f"signal:{signal.id}",
        side=side,
        qty_lots=qty_lots,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        trail_activation_fraction=trail_activation_fraction,
        trail_lock_fraction=trail_lock_fraction,
        structure_level=structure_level,
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(trade_intent)
    db.flush()
    return trade_intent


# -- dispatch_trade_intent ----------------------------------------------------


class _RealBrokerNeverCalled:
    """Stands in for a connected real broker (e.g. Shoonya, installed via
    `composition.set_broker` exactly as `api.v1.shoonya.oauth_callback`
    does). Any call proves paper execution routed to whichever broker
    `get_broker()` currently resolves to, instead of the persistent
    execution mock `get_execution_broker` must always use today.
    """

    def place_order(self, request):  # noqa: ANN001, ARG002 - deliberately never valid to call
        raise AssertionError(
            "a 'connected' broker's place_order was called for a paper trade — "
            "get_execution_broker isn't isolating execution from whichever broker "
            "composition.get_broker() currently resolves to"
        )

    def get_positions(self):
        raise AssertionError("a 'connected' broker's get_positions was called for a paper trade")


def test_dispatch_ignores_connected_real_broker_for_paper_session(
    db: Session, trading_session, strategy_run, option_contract
):
    """Regression test for the bug where connecting a real broker (Shoonya)
    silently caused paper execution's default broker resolution to route
    through it instead of the persistent execution mock — deliberately does
    NOT pass `broker=` to `dispatch_trade_intent`, since that's the exact
    default-resolution path that was broken.
    """
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    composition.set_broker(_RealBrokerNeverCalled())  # type: ignore[arg-type]
    try:
        order = dispatch_trade_intent(db, trading_session, trade_intent)
    finally:
        composition.set_broker(None)

    assert order.status == OrderStatus.FILLED


def test_dispatch_creates_order_position_stop_and_trail(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)

    order = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)

    assert order.status == OrderStatus.FILLED
    assert order.qty == 25  # qty_lots(1) x lot_size(25)
    assert order.trade_intent_id == trade_intent.id

    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    assert position.status == PositionStatus.OPEN
    assert position.qty == 25
    assert position.entry_price == pytest.approx(_price(order.avg_fill_price))

    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()
    assert float(stop_plan.stop_price) == pytest.approx(72.0)
    assert stop_plan.qty == 25

    trail_plan = db.query(TrailPlan).filter(TrailPlan.position_id == position.id).one()
    assert trail_plan.status == TrailPlanStatus.INACTIVE
    # Activation is halfway from entry to target (12.0 wide -> 6.0 in).
    assert float(trail_plan.activation_price) == pytest.approx(
        _price(order.avg_fill_price) + 6.0
    )


def test_dispatch_fills_at_the_trade_intents_own_proposed_price(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """The Stage 1 fix's own acceptance criterion: the fill price (and thus
    Position.entry_price) must equal what the strategy actually proposed
    (TradeIntent.entry_price), not MockBrokerAdapter's independent
    synthetic price for this symbol -- proven by seeding the mock's own
    price to something else entirely and confirming it has no effect.
    """
    broker._prices[option_contract.symbol] = 999.0  # noqa: SLF001 - must be ignored
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, entry_price=80.0
    )

    order = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)

    assert _price(order.avg_fill_price) == pytest.approx(80.0)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    assert float(position.entry_price) == pytest.approx(float(trade_intent.entry_price))


def test_dispatch_uses_per_strategy_trail_fractions_when_set(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0,
        trail_activation_fraction=0.25, trail_lock_fraction=0.75,
    )

    order = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    trail_plan = db.query(TrailPlan).filter(TrailPlan.position_id == position.id).one()

    # 0.25 of the 12.0-wide entry->target distance, not the generic 0.5.
    assert float(trail_plan.activation_price) == pytest.approx(
        _price(order.avg_fill_price) + 3.0
    )
    assert float(trail_plan.trail_value) == pytest.approx(0.75)


def test_dispatch_is_idempotent_on_trade_intent_key(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)

    first = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    second = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)

    assert first.id == second.id
    assert db.query(Order).filter(Order.trade_intent_id == trade_intent.id).count() == 1
    assert db.query(Position).filter(Position.trade_intent_id == trade_intent.id).count() == 1


def test_dispatch_sends_market_order_for_a_paper_dispatch(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """2026-08-19 regression: paper stays exactly as it always was — an
    explicit-broker dispatch (this suite's normal paper pattern) must never
    be re-priced by the new live-only limit-order buffer.
    """
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, entry_price=80.0
    )

    order = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)

    assert order.order_type == OrderType.MARKET
    assert _price(order.avg_fill_price) == pytest.approx(80.0)


def test_dispatch_sends_buffered_limit_order_for_a_genuinely_live_dispatch(
    db: Session, broker, workspace, strategy_run, strategy_config, trading_session,
    option_contract, monkeypatch,
):
    """2026-08-19 fix: a real Shoonya PlaceOrder rejects order_type=MARKET
    outright ("ALGO_CHK: MKT Order type not allowed for API order",
    live-confirmed) -- a genuinely live-routed dispatch must send LIMIT with
    a real, non-zero price buffer (APP_LIVE_LIMIT_ORDER_BUFFER_PCT, default
    0.5%) so it still fills despite LTP moving between decision and
    placement. Same live-routing/_FakeLiveBroker/preflight-neutralizing
    pattern as `test_close_position_updates_session_pnl_and_can_trigger_kill_switch`.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()

    class _FakeLiveBroker:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

    fake_live_broker = _FakeLiveBroker(broker)
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.get_execution_broker",
        lambda trading_session, strategy_run=None, **kwargs: fake_live_broker,
    )
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.run_preflight_checks",
        lambda *args, **kwargs: None,
    )

    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, entry_price=80.0
    )
    order = dispatch_trade_intent(db, trading_session, trade_intent)

    assert order.mode == OrderMode.LIVE
    assert order.order_type == OrderType.LIMIT
    # BUY side, 0.5% default buffer -- priced *above* entry_price so a real
    # limit order still tolerates adverse LTP movement and fills.
    assert _price(order.avg_fill_price) == pytest.approx(80.0 * 1.005, rel=1e-3)


def test_dispatch_marks_trade_intent_expired_on_synchronous_rejection(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """2026-08-20 live incident's original half: a *synchronously* rejected
    order (place_order returns REJECTED immediately, the common case for a
    real broker rejecting bad parameters outright) left TradeIntent stuck
    at DISPATCHED forever -- the exact bug that permanently blocked
    `_same_strike_locked` for a real strategy+contract until a manual DB
    fix. `reconcile_pending_live_orders` already covers the *asynchronous*
    version of this (a pending order later discovered rejected); this pins
    the synchronous one in `dispatch_trade_intent` itself.
    """
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    broker.queue_fill_scenario(
        option_contract.symbol, FillScenario(status=BrokerOrderStatus.REJECTED)
    )

    order = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)

    assert order.status == OrderStatus.REJECTED
    db.refresh(trade_intent)
    assert trade_intent.status == TradeIntentStatus.EXPIRED
    assert db.query(Position).filter(Position.trade_intent_id == trade_intent.id).count() == 0


# -- reconcile_pending_live_orders --------------------------------------------


class _FakeOrderStatusBroker:
    """Minimal `BrokerPort`-shaped double for `reconcile_pending_live_orders`
    — a scripted `get_order_status` result, plus an optional
    `peek_cached_order_update` to exercise the WS-cache fast path
    (mirroring `ShoonyaBrokerAdapter`'s real one without needing a live WS).
    """

    def __init__(
        self,
        *,
        rest_result: OrderResult | None = None,
        cached_result: OrderResult | None = None,
        rest_raises: Exception | None = None,
    ) -> None:
        self._rest_result = rest_result
        self._cached_result = cached_result
        self._rest_raises = rest_raises
        self.get_order_status_calls: list[str] = []

    def get_order_status(self, broker_order_id: str) -> OrderResult:
        self.get_order_status_calls.append(broker_order_id)
        if self._rest_raises is not None:
            raise self._rest_raises
        assert self._rest_result is not None
        return self._rest_result

    def peek_cached_order_update(self, broker_order_id: str) -> OrderResult | None:
        return self._cached_result


def _make_pending_live_order(
    db: Session,
    trading_session: TradingSession,
    trade_intent: TradeIntent,
    option_contract: OptionContract,
    *,
    broker_order_id: str = "BROKER-ORD-1",
) -> Order:
    now = datetime.now(UTC)
    order = Order(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=trade_intent.id,
        idempotency_key=trade_intent.idempotency_key,
        mode=OrderMode.LIVE,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=25,
        status=OrderStatus.PENDING,
        filled_qty=0,
        avg_fill_price=None,
        broker_order_id=broker_order_id,
        submitted_at=now,
        updated_at=now,
    )
    db.add(order)
    db.flush()
    return order


def test_reconcile_pending_live_orders_creates_position_on_ws_cached_fill(
    db: Session, trading_session, strategy_run, option_contract
):
    """The fast path: a WS-pushed fill sitting in the cache is acted on
    immediately, with zero REST calls, regardless of `allow_rest_fallback`.
    """
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order = _make_pending_live_order(db, trading_session, trade_intent, option_contract)
    fake_broker = _FakeOrderStatusBroker(
        cached_result=OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id=order.broker_order_id,
            status=BrokerOrderStatus.FILLED,
            filled_qty=25,
            avg_fill_price=81.0,
        )
    )

    reconcile_pending_live_orders(
        db, trading_session, allow_rest_fallback=False, broker=fake_broker  # type: ignore[arg-type]
    )

    db.refresh(order)
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 25
    assert fake_broker.get_order_status_calls == []
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    assert position.status == PositionStatus.OPEN
    assert position.qty == 25


def test_reconcile_pending_live_orders_does_not_poll_rest_when_not_allowed(
    db: Session, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order = _make_pending_live_order(db, trading_session, trade_intent, option_contract)
    fake_broker = _FakeOrderStatusBroker()  # no cache, no scripted REST result

    reconcile_pending_live_orders(
        db, trading_session, allow_rest_fallback=False, broker=fake_broker  # type: ignore[arg-type]
    )

    db.refresh(order)
    assert order.status == OrderStatus.PENDING
    assert fake_broker.get_order_status_calls == []
    assert db.query(Position).filter(Position.trade_intent_id == trade_intent.id).count() == 0


def test_reconcile_pending_live_orders_falls_back_to_rest_when_allowed(
    db: Session, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order = _make_pending_live_order(db, trading_session, trade_intent, option_contract)
    fake_broker = _FakeOrderStatusBroker(
        rest_result=OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id=order.broker_order_id,
            status=BrokerOrderStatus.FILLED,
            filled_qty=25,
            avg_fill_price=81.0,
        )
    )

    reconcile_pending_live_orders(
        db, trading_session, allow_rest_fallback=True, broker=fake_broker  # type: ignore[arg-type]
    )

    db.refresh(order)
    assert order.status == OrderStatus.FILLED
    assert fake_broker.get_order_status_calls == [order.broker_order_id]
    assert db.query(Position).filter(Position.trade_intent_id == trade_intent.id).count() == 1


def test_reconcile_pending_live_orders_marks_trade_intent_expired_on_rejection(
    db: Session, trading_session, strategy_run, option_contract
):
    """2026-08-20 live incident's other half: a pending order that
    ultimately gets REJECTED (not filled) must not leave TradeIntent stuck
    at DISPATCHED forever, or `_same_strike_locked` blocks this exact
    contract for this strategy permanently — the same manual-DB-fix
    incident this same night, just discovered the other way round.
    """
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order = _make_pending_live_order(db, trading_session, trade_intent, option_contract)
    fake_broker = _FakeOrderStatusBroker(
        rest_result=OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id=order.broker_order_id,
            status=BrokerOrderStatus.REJECTED,
            filled_qty=0,
            avg_fill_price=None,
            raw_message="margin insufficient",
        )
    )

    reconcile_pending_live_orders(
        db, trading_session, allow_rest_fallback=True, broker=fake_broker  # type: ignore[arg-type]
    )

    db.refresh(order)
    db.refresh(trade_intent)
    assert order.status == OrderStatus.REJECTED
    assert trade_intent.status == TradeIntentStatus.EXPIRED
    assert db.query(Position).filter(Position.trade_intent_id == trade_intent.id).count() == 0


def test_reconcile_pending_live_orders_ignores_a_still_non_terminal_status(
    db: Session, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order = _make_pending_live_order(db, trading_session, trade_intent, option_contract)
    fake_broker = _FakeOrderStatusBroker(
        rest_result=OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id=order.broker_order_id,
            status=BrokerOrderStatus.OPEN,
            filled_qty=0,
            avg_fill_price=None,
        )
    )

    reconcile_pending_live_orders(
        db, trading_session, allow_rest_fallback=True, broker=fake_broker  # type: ignore[arg-type]
    )

    db.refresh(order)
    db.refresh(trade_intent)
    assert order.status == OrderStatus.PENDING
    assert trade_intent.status == TradeIntentStatus.DISPATCHED


def test_reconcile_pending_live_orders_continues_after_one_orders_broker_error(
    db: Session, trading_session, strategy_run, option_contract
):
    """A `get_order_status` failure for one order must not stop the loop
    from resolving other pending orders in the same cycle.
    """
    intent_a = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order_a = _make_pending_live_order(
        db, trading_session, intent_a, option_contract, broker_order_id="ORD-A"
    )
    intent_b = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order_b = _make_pending_live_order(
        db, trading_session, intent_b, option_contract, broker_order_id="ORD-B"
    )

    class _MixedBroker:
        def get_order_status(self, broker_order_id: str) -> OrderResult:
            if broker_order_id == "ORD-A":
                raise BrokerError("transient failure")
            return OrderResult(
                idempotency_key=order_b.idempotency_key,
                broker_order_id="ORD-B",
                status=BrokerOrderStatus.FILLED,
                filled_qty=25,
                avg_fill_price=81.0,
            )

    reconcile_pending_live_orders(
        db, trading_session, allow_rest_fallback=True, broker=_MixedBroker()  # type: ignore[arg-type]
    )

    db.refresh(order_a)
    db.refresh(order_b)
    assert order_a.status == OrderStatus.PENDING
    assert order_b.status == OrderStatus.FILLED


# -- close_position -----------------------------------------------------------


def test_close_position_computes_pnl_and_slippage_on_stop(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    entry_price = _price(order.avg_fill_price)

    outcome = close_position(
        db, trading_session, position, ExitReason.STOP, intended_price=72.0, broker=broker
    )

    assert outcome is not None
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED
    assert position.closing_order_id is not None

    # Exit fills at intended_price itself (0.0 default slippage in this
    # test, PAPER_FILL_SLIPPAGE_PCT unset) -- the fix this test now pins:
    # close_position's exit order fills at the price that actually justified
    # the exit, not at MockBrokerAdapter's own independent synthetic price
    # (previously forced here via a broker._prices[...] reach-in that no
    # longer has any effect on the fill, by design). See
    # test_apply_slippage_direction (tests/unit) for the nonzero-slippage
    # case.
    exit_fill_price = float(outcome.exit_price)
    assert exit_fill_price == pytest.approx(72.0)
    assert outcome.realized_pnl == pytest.approx((exit_fill_price - entry_price) * 25)
    assert outcome.slippage == pytest.approx(0.0)

    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()
    assert stop_plan.status == "triggered"


def test_close_position_routes_realized_pnl_and_slippage_through_the_shared_signed_pnl(
    db: Session, broker, trading_session, strategy_run, option_contract, monkeypatch
):
    """`_finalize_position_close`'s `realized_pnl`/`slippage` used to
    hand-derive the sign convention independently (see `app.core.pnl`'s own
    module docstring for the three-way duplication this closed). Pins that
    it now genuinely calls the shared `app.core.pnl.signed_pnl` -- not just
    a formula that happens to still match it -- by monkeypatching the name
    imported into this module and asserting it's actually invoked (twice:
    once for realized_pnl, once for slippage). Against the pre-fix code this
    assertion would fail outright, since `signed_pnl` was never imported or
    called there at all.
    """
    import app.modules.execution_engine.paper.service as paper_service_module

    calls: list[tuple] = []
    real_signed_pnl = paper_service_module.signed_pnl

    def _spy(entry_price, other_price, qty, side):
        calls.append((entry_price, other_price, qty, side))
        return real_signed_pnl(entry_price, other_price, qty, side)

    monkeypatch.setattr(paper_service_module, "signed_pnl", _spy)

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    outcome = close_position(
        db, trading_session, position, ExitReason.STOP, intended_price=72.0, broker=broker
    )

    assert outcome is not None
    # realized_pnl(entry, exit) and slippage(intended, exit) -- both routed
    # through the same shared function.
    assert len(calls) == 2


def test_close_position_is_idempotent_when_already_closed(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    first = close_position(
        db, trading_session, position, ExitReason.MANUAL, intended_price=80.0, broker=broker
    )
    assert first is not None

    second = close_position(
        db, trading_session, position, ExitReason.MANUAL, intended_price=80.0, broker=broker
    )
    assert second is None
    assert db.query(Order).filter(Order.position_id == position.id).count() == 1


def test_close_position_leaves_position_open_when_exit_order_is_rejected(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """The narrowly-scoped fix: a non-FILLED exit order must not crash
    computing exit_price, and must not lie about the position being closed
    — it stays OPEN so the next PositionManager cycle or a manual reconcile
    can still see and retry it. Normal exits (every other test in this file)
    are unaffected since they never queue a fault scenario.
    """
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    broker.queue_fill_scenario(
        option_contract.symbol, FillScenario(status=BrokerOrderStatus.REJECTED)
    )

    outcome = close_position(
        db, trading_session, position, ExitReason.MANUAL, intended_price=80.0, broker=broker
    )

    assert outcome is None
    db.refresh(position)
    assert position.status == PositionStatus.OPEN
    assert position.closing_order_id is None

    exit_order = (
        db.query(Order)
        .filter(Order.position_id == position.id, Order.status == OrderStatus.REJECTED)
        .one()
    )
    assert exit_order.avg_fill_price is None

    alert = (
        db.query(SystemAlert)
        .filter(SystemAlert.trading_session_id == trading_session.id)
        .filter(SystemAlert.category == "exit_order_unfilled")
        .one()
    )
    assert str(position.id) in alert.message


def test_close_position_updates_session_pnl_and_can_trigger_kill_switch(
    db: Session, broker, workspace, strategy_run, strategy_config, trading_session,
    option_contract, monkeypatch,
):
    """2026-08-19: `record_trade_outcome_effects` only applies session P&L
    effects for a genuinely *live* close now (the most severe gap from
    that day's audit — paper losses used to trip a real kill_switch). The
    entry stays an explicit-broker paper dispatch (irrelevant to this
    test); the close is what needs to resolve live, so it's called without
    an explicit broker, through a graduated strategy on a guarded-live
    session, with `get_execution_broker`/preflight neutralized the same
    way `test_evaluate_trade_intent_margin_check_failed`-style tests
    already do elsewhere in this suite.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    trading_session.daily_loss_cap = 1.0  # trivially small so any loss breaches it
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    # Force a real loss (see the comment in the slippage test above for why
    # this is otherwise a no-op price move).
    broker._prices[option_contract.symbol] = _price(order.avg_fill_price) - 5.0  # noqa: SLF001

    # is_execution_broker_live is a plain `not isinstance(_, MockBrokerAdapter)`
    # check -- returning the `broker` fixture itself would still read as
    # paper. A thin delegate that isn't a MockBrokerAdapter subclass reads
    # as live while all real behavior (get_quote/place_order/...) still
    # goes through the same seeded mock underneath.
    class _FakeLiveBroker:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

    fake_live_broker = _FakeLiveBroker(broker)
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.get_execution_broker",
        lambda trading_session, strategy_run=None, **kwargs: fake_live_broker,
    )
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.run_preflight_checks",
        lambda *args, **kwargs: None,
    )

    outcome = close_position(db, trading_session, position, ExitReason.STOP, intended_price=72.0)

    assert outcome is not None
    assert outcome.realized_pnl < 0
    # record_trade_outcome_effects (risk_engine.service) is what updates
    # these — this is Phase 3's real replacement for
    # record_synthetic_outcome, now fed by an actual fill instead of a
    # random P&L.
    assert float(trading_session.cumulative_realized_pnl) == pytest.approx(outcome.realized_pnl)
    assert trading_session.consecutive_losses == 1
    assert trading_session.mode == SafeMode.KILL_SWITCH

    # 2026-08-19: the exit order for a genuinely live close must also be a
    # buffered LIMIT order, same fix and reasoning as the entry side.
    exit_order = db.query(Order).filter(Order.position_id == position.id).one()
    assert exit_order.order_type == OrderType.LIMIT
    assert exit_order.mode == OrderMode.LIVE


def test_run_single_position_square_off_routes_a_live_positions_exit_through_its_own_broker(
    db: Session, broker, workspace, strategy_run, strategy_config, trading_session,
    option_contract, monkeypatch,
):
    """`api.v1.execution.square_off_position` (the new single-position manual
    square-off endpoint) calls `run_single_position_square_off`, not
    `close_position` directly — pins that the exit for a position resolved
    to a non-mock broker (`resolve_broker_for_position`) actually places its
    exit order through *that* broker, not silently through
    `get_execution_mock()`'s default.

    **2026-08-20: this used to document a known pre-existing gap; now pins
    the fix instead.** `eod_square_off.run_single_position_square_off` (same
    as `_square_off_all_open_positions` before it) pre-resolves the broker
    once (needed to price *and* close with the same instance) and passes it
    into `close_position` explicitly — unlike `PositionManager`'s stop/
    target/trail path, which used to call `close_position(..., broker=None)`
    specifically so its own internal resolution could "detect live"
    (`is_execution_broker_live`) without tripping the old `broker_was_
    provided` guard. That guard used to force every explicit-broker exit
    `OrderMode.PAPER` and a MARKET order regardless of whether the broker
    actually used was real — it couldn't distinguish "caller pre-resolved
    the correct broker" (this endpoint, EOD/margin-breach square-off, the
    `POST /sessions/{id}/square-off` manual endpoint) from "caller injected
    a test fake". Removed: `order_mode` is now decided purely by
    `is_execution_broker_live(broker)`, whether or not `broker=` was passed
    explicitly — see `close_position`'s own docstring. This test now proves
    the real consequence that used to be silently wrong: a genuinely live
    position's manual square-off gets real PnL/consecutive-loss/kill_switch
    effects and a LIMIT order (MARKET is what live Shoonya rejects for API
    orders), not a paper-tagged MARKET fill.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    class _FakeLiveBroker:
        def __init__(self, inner):
            self._inner = inner
            self.place_order_calls = 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def place_order(self, request):
            self.place_order_calls += 1
            return self._inner.place_order(request)

    fake_live_broker = _FakeLiveBroker(broker)
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.get_execution_broker",
        lambda trading_session, strategy_run=None, **kwargs: fake_live_broker,
    )
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.run_preflight_checks",
        lambda *args, **kwargs: None,
    )

    outcome = run_single_position_square_off(
        db, None, trading_session, position, ExitReason.MANUAL
    )

    assert outcome is not None
    assert outcome.exit_reason == ExitReason.MANUAL
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED

    # The exit order really went through the position's own resolved
    # broker (fake_live_broker), not a different/default one.
    assert fake_live_broker.place_order_calls == 1
    exit_order = db.query(Order).filter(Order.position_id == position.id).one()
    assert exit_order.status == OrderStatus.FILLED
    # The actual fix this test now pins: an explicitly-passed but genuinely
    # live broker must still be tagged LIVE (LIMIT order, real preflight
    # gate), not force-tagged PAPER just because it was pre-resolved and
    # passed in rather than resolved internally.
    assert exit_order.mode == OrderMode.LIVE
    assert exit_order.order_type == OrderType.LIMIT


def test_run_single_position_square_off_still_tags_a_mock_brokers_exit_as_paper(
    db: Session, broker, workspace, strategy_run, strategy_config, trading_session,
    option_contract,
):
    """The other direction of the 2026-08-20 fix above: `broker=` being
    explicitly passed through `eod_square_off.py`'s resolution chain must
    not, by itself, make an exit read as live -- only a broker that's
    genuinely not a `MockBrokerAdapter` should. No monkeypatching here:
    `resolve_broker_for_position` resolves the real (mock) broker itself,
    same as every production call with no `strategy_run`/session override.
    """
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    outcome = run_single_position_square_off(
        db, broker, trading_session, position, ExitReason.MANUAL
    )

    assert outcome is not None
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED

    exit_order = db.query(Order).filter(Order.position_id == position.id).one()
    assert exit_order.mode == OrderMode.PAPER
    assert exit_order.order_type == OrderType.MARKET
    # A paper close must never touch session-level real-money effects.
    assert float(trading_session.cumulative_realized_pnl) == 0.0
    assert trading_session.consecutive_losses == 0


def test_run_single_position_square_off_raises_on_unresolvable_option_contract(
    db: Session, broker, trading_session
):
    """QC fix #4: `run_single_position_square_off` used to fold "the
    position's own option_contract_id doesn't resolve to a real
    OptionContract row" into the same `None` return as "exit order didn't
    fill synchronously" -- a data-integrity problem silently
    indistinguishable from a normal timing outcome. It must now raise
    `UnresolvableOptionContractError` instead. `broker=broker` is passed
    explicitly so `resolve_broker_for_position` (which needs a real
    `trade_intent_id` lookup) is never reached -- a bare stand-in with just
    `option_contract_id` set is enough to exercise the early-return branch,
    and avoids needing an actually-corrupt DB row (the real FK constraint
    on `positions.option_contract_id` would reject that outright, same as
    production data can never really get into this state via a normal
    write).
    """
    from types import SimpleNamespace

    fake_position = SimpleNamespace(option_contract_id=uuid.uuid4())

    with pytest.raises(UnresolvableOptionContractError) as exc_info:
        run_single_position_square_off(
            db, broker, trading_session, fake_position, ExitReason.MANUAL  # type: ignore[arg-type]
        )
    assert exc_info.value.option_contract_id == fake_position.option_contract_id


def test_square_off_all_open_positions_skips_a_corrupt_position_and_closes_the_rest(
    db: Session, broker, trading_session, strategy_run, option_contract, monkeypatch
):
    """The EOD/margin-breach batch sweep (`_square_off_all_open_positions`,
    exercised here via `run_eod_square_off`) must not let one position's
    `UnresolvableOptionContractError` block the rest of the session's forced
    flatten -- same effective "skip and continue" behavior this sweep
    already had before the exception existed (when this case silently
    returned `None`), just no longer silent. `position_a`'s failure is
    simulated by monkeypatching `run_single_position_square_off` itself
    (real corrupt FK data isn't constructible -- see the test above), while
    `position_b` goes through the real, unmocked function and must still
    close normally.
    """
    import app.modules.scheduler.eod_square_off as eod_square_off_module

    trade_intent_a = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent_a, broker=broker)
    position_a = db.query(Position).filter(Position.trade_intent_id == trade_intent_a.id).one()

    trade_intent_b = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent_b, broker=broker)
    position_b = db.query(Position).filter(Position.trade_intent_id == trade_intent_b.id).one()

    real_run_single = eod_square_off_module.run_single_position_square_off

    def _fake_run_single(db_, broker_, trading_session_, position, exit_reason, **kwargs):
        if position.id == position_a.id:
            raise UnresolvableOptionContractError(position.option_contract_id)
        return real_run_single(db_, broker_, trading_session_, position, exit_reason, **kwargs)

    monkeypatch.setattr(eod_square_off_module, "run_single_position_square_off", _fake_run_single)

    outcomes = run_eod_square_off(db, broker, trading_session)

    assert len(outcomes) == 1
    assert outcomes[0].position_id == position_b.id

    db.refresh(position_a)
    db.refresh(position_b)
    assert position_a.status == PositionStatus.OPEN
    assert position_b.status == PositionStatus.CLOSED


def test_close_position_does_not_update_session_pnl_for_a_paper_close(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """2026-08-19 regression: the actual bug this fixed. A paper close
    (this test's normal, explicit-broker setup, unchanged) must never touch
    `cumulative_realized_pnl`/`consecutive_losses` or trip `kill_switch` —
    proven here with a loss well past a trivially small daily_loss_cap.
    """
    trading_session.daily_loss_cap = 1.0
    db.add(trading_session)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    broker._prices[option_contract.symbol] = _price(order.avg_fill_price) - 5.0  # noqa: SLF001

    outcome = close_position(
        db, trading_session, position, ExitReason.STOP, intended_price=72.0, broker=broker
    )

    assert outcome is not None
    assert outcome.realized_pnl < 0
    assert float(trading_session.cumulative_realized_pnl) == 0.0
    assert trading_session.consecutive_losses == 0
    assert trading_session.mode == SafeMode.PAPER_ONLY

    exit_order = db.query(Order).filter(Order.position_id == position.id).one()
    assert exit_order.order_type == OrderType.MARKET
    assert exit_order.mode == OrderMode.PAPER


# -- reconcile_pending_live_exit_orders ---------------------------------------


def _open_live_position(
    db: Session, broker, trading_session, strategy_run, option_contract
) -> Position:
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    position_id = position.id
    # Real LIVE mode, not the broker fixture's default paper-shaped fill --
    # reconcile_pending_live_exit_orders only ever looks at mode=LIVE
    # orders, matching production (a PAPER exit always fills synchronously).
    db.query(Order).filter(Order.trade_intent_id == trade_intent.id).update(
        {"mode": OrderMode.LIVE}
    )
    db.flush()
    refreshed = db.get(Position, position_id)
    assert refreshed is not None
    return refreshed


def _make_pending_live_exit_order(
    db: Session, trading_session: TradingSession, position: Position, *, broker_order_id: str
) -> Order:
    now = datetime.now(UTC)
    order = Order(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=position.option_contract_id,
        position_id=position.id,
        idempotency_key=f"exit:{position.id}",
        mode=OrderMode.LIVE,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        qty=position.qty,
        status=OrderStatus.PENDING,
        filled_qty=0,
        avg_fill_price=None,
        broker_order_id=broker_order_id,
        submitted_at=now,
        updated_at=now,
    )
    db.add(order)
    db.flush()
    return order


def test_reconcile_pending_live_exit_orders_closes_position_on_ws_cached_fill(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    position = _open_live_position(db, broker, trading_session, strategy_run, option_contract)
    exit_order = _make_pending_live_exit_order(
        db, trading_session, position, broker_order_id="EXIT-1"
    )
    fake_broker = _FakeOrderStatusBroker(
        cached_result=OrderResult(
            idempotency_key=exit_order.idempotency_key,
            broker_order_id=exit_order.broker_order_id,
            status=BrokerOrderStatus.FILLED,
            filled_qty=position.qty,
            avg_fill_price=90.0,
        )
    )

    reconcile_pending_live_exit_orders(
        db, trading_session, allow_rest_fallback=False, broker=fake_broker  # type: ignore[arg-type]
    )

    db.refresh(exit_order)
    db.refresh(position)
    assert exit_order.status == OrderStatus.FILLED
    assert fake_broker.get_order_status_calls == []
    assert position.status == PositionStatus.CLOSED
    assert position.closing_order_id == exit_order.id

    outcome = db.query(TradeOutcome).filter(TradeOutcome.position_id == position.id).one()
    assert outcome.exit_reason == ExitReason.RECONCILED
    assert float(outcome.exit_price) == pytest.approx(90.0)
    # No original trigger price survives to a late-discovered fill -- 0.0
    # slippage is the honest answer, not a fabricated comparison.
    assert outcome.slippage == pytest.approx(0.0)

    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()
    assert stop_plan.status == "cancelled"


def test_reconcile_pending_live_exit_orders_falls_back_to_rest_when_allowed(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    position = _open_live_position(db, broker, trading_session, strategy_run, option_contract)
    exit_order = _make_pending_live_exit_order(
        db, trading_session, position, broker_order_id="EXIT-2"
    )
    fake_broker = _FakeOrderStatusBroker(
        rest_result=OrderResult(
            idempotency_key=exit_order.idempotency_key,
            broker_order_id=exit_order.broker_order_id,
            status=BrokerOrderStatus.FILLED,
            filled_qty=position.qty,
            avg_fill_price=90.0,
        )
    )

    reconcile_pending_live_exit_orders(
        db, trading_session, allow_rest_fallback=True, broker=fake_broker  # type: ignore[arg-type]
    )

    db.refresh(position)
    assert fake_broker.get_order_status_calls == [exit_order.broker_order_id]
    assert position.status == PositionStatus.CLOSED


def test_reconcile_pending_live_exit_orders_does_not_poll_rest_when_not_allowed(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    position = _open_live_position(db, broker, trading_session, strategy_run, option_contract)
    _make_pending_live_exit_order(db, trading_session, position, broker_order_id="EXIT-3")
    fake_broker = _FakeOrderStatusBroker()  # no cache, no scripted REST result

    reconcile_pending_live_exit_orders(
        db, trading_session, allow_rest_fallback=False, broker=fake_broker  # type: ignore[arg-type]
    )

    db.refresh(position)
    assert fake_broker.get_order_status_calls == []
    assert position.status == PositionStatus.OPEN


def test_reconcile_pending_live_exit_orders_leaves_position_open_on_rejection(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    position = _open_live_position(db, broker, trading_session, strategy_run, option_contract)
    exit_order = _make_pending_live_exit_order(
        db, trading_session, position, broker_order_id="EXIT-4"
    )
    fake_broker = _FakeOrderStatusBroker(
        rest_result=OrderResult(
            idempotency_key=exit_order.idempotency_key,
            broker_order_id=exit_order.broker_order_id,
            status=BrokerOrderStatus.REJECTED,
            filled_qty=0,
            avg_fill_price=None,
        )
    )

    reconcile_pending_live_exit_orders(
        db, trading_session, allow_rest_fallback=True, broker=fake_broker  # type: ignore[arg-type]
    )

    db.refresh(exit_order)
    db.refresh(position)
    assert exit_order.status == OrderStatus.REJECTED
    assert position.status == PositionStatus.OPEN
    assert position.closing_order_id is None
    assert db.query(TradeOutcome).filter(TradeOutcome.position_id == position.id).count() == 0


# -- evaluate_open_position: stop/target/trail --------------------------------


def test_evaluate_open_position_exits_on_stop_hit(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    outcome = evaluate_open_position(db, trading_session, position, tick_price=71.0, broker=broker)

    assert outcome is not None
    assert outcome.exit_reason == ExitReason.STOP
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED


def test_evaluate_open_position_exits_on_target_hit(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    outcome = evaluate_open_position(db, trading_session, position, tick_price=93.0, broker=broker)

    assert outcome is not None
    assert outcome.exit_reason == ExitReason.TARGET


def test_evaluate_open_position_no_exit_when_price_between_stop_and_target(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    outcome = evaluate_open_position(db, trading_session, position, tick_price=81.0, broker=broker)

    assert outcome is None
    db.refresh(position)
    assert position.status == PositionStatus.OPEN


def test_evaluate_open_position_trail_activates_then_triggers(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    # entry/stop/target are deliberately anchored to the mock adapter's own
    # deterministic price for this symbol (not an arbitrary 80.0) — the mock
    # seeds a symbol's base price independently of whatever a test hardcodes
    # as "entry_price" on the TradeIntent, so a stop/target picked without
    # regard for that base price can end up already on the wrong side of it
    # (e.g. stop_price above the actual fill price), which is exactly what
    # broke this test's first version.
    real_price = broker._price_for(option_contract.symbol)  # noqa: SLF001
    entry_price = real_price
    stop_price = real_price - 8.0
    target_price = real_price + 20.0

    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=entry_price, stop_price=stop_price, target_price=target_price,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    trail_plan = db.query(TrailPlan).filter(TrailPlan.position_id == position.id).one()
    activation_price = float(trail_plan.activation_price)  # entry + 10 (50% of the 20-wide range)
    assert activation_price == pytest.approx(entry_price + 10.0)

    # 1. Price reaches activation — trail activates, locks in 0 (exactly at
    # activation), no exit yet.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=activation_price, broker=broker
    )
    assert outcome is None
    db.refresh(trail_plan)
    assert trail_plan.status == TrailPlanStatus.ACTIVE
    assert _price(trail_plan.current_stop_price) == pytest.approx(activation_price)

    # 2. Price advances further (+6 beyond activation) — trail tightens to
    # lock in half of that (+3), still no exit.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=activation_price + 6.0, broker=broker
    )
    assert outcome is None
    db.refresh(trail_plan)
    assert _price(trail_plan.current_stop_price) == pytest.approx(activation_price + 3.0)

    # 3. Price pulls back through the trailed stop — exits via TRAIL, not STOP.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=_price(trail_plan.current_stop_price) - 0.5,
        broker=broker,
    )
    assert outcome is not None
    assert outcome.exit_reason == ExitReason.TRAIL


def test_evaluate_open_position_exits_on_structure_break(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    # structure_level=22000 for a BUY (CE) position: the underlying falling
    # back below the opening-range low it broke out of invalidates the
    # setup, independent of whether the option premium has hit its own stop.
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0, structure_level=22000.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    # Price is comfortably between stop and target on the premium side —
    # only the underlying has broken structure.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21990.0
    )

    assert outcome is not None
    assert outcome.exit_reason == ExitReason.STRUCTURE_BREAK
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED


def test_evaluate_open_position_no_structure_break_exit_without_underlying_price(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0, structure_level=22000.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    # No underlying_price supplied (mirrors every pre-Phase-4 caller) — the
    # structure-break check must be a no-op, not raise or false-trigger.
    outcome = evaluate_open_position(db, trading_session, position, tick_price=82.0, broker=broker)

    assert outcome is None


def test_evaluate_open_position_exits_on_spread_blowout(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    # Price still comfortably between stop and target, but the spread has
    # blown out well past SPREAD_BLOWOUT_PCT (0.30) of the current price.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, bid=70.0, ask=95.0
    )

    assert outcome is not None
    assert outcome.exit_reason == ExitReason.SPREAD_BLOWOUT


def test_evaluate_open_position_no_spread_blowout_exit_within_tolerance(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, bid=81.5, ask=82.5
    )

    assert outcome is None
