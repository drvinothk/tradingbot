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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

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
from app.domain.market.models import Instrument, OptionContract, OptionType, PriceBar
from app.domain.ops.models import AlertSeverity, SystemAlert
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import (
    ExecutionMode,
    Signal,
    SignalSide,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.broker_adapter import composition
from app.modules.broker_adapter.base.contracts import (
    BrokerOrderStatus,
    OrderRequest,
    OrderResult,
    TradeFill,
)
from app.modules.broker_adapter.base.contracts import OrderSide as ContractOrderSide
from app.modules.broker_adapter.base.errors import BrokerError, ConfigurationError
from app.modules.broker_adapter.mock.adapter import FillScenario, MockBrokerAdapter
from app.modules.execution_engine.paper.service import (
    close_position,
    close_position_from_external_fill,
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
from app.modules.strategy_engine.common_rules import BAR_TIMEFRAME

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
def option_contract_pe(db: Session, instrument: Instrument) -> OptionContract:
    """PE sibling of `option_contract` -- every existing structure_break
    test in this file uses the CE-only fixture above, which is exactly why
    the 2026-09-01 direction bug (`favorable = side == SignalSide.BUY`,
    always True since every position here is BUY, reused for the
    underlying-based structure check where favorable direction depends on
    CE vs PE) shipped and stayed live undetected -- zero test coverage ever
    exercised a PE structure-break at all."""
    contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
        strike=22000,
        option_type=OptionType.PE,
        symbol="NIFTY26JUL22000PE-EXEC",
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
    structure_break_buffer: float | None = None,
    structure_break_persistence_seconds: float | None = None,
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
        structure_break_buffer=structure_break_buffer,
        structure_break_persistence_seconds=structure_break_persistence_seconds,
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
        structure_break_buffer=structure_break_buffer,
        structure_break_persistence_seconds=structure_break_persistence_seconds,
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(trade_intent)
    db.flush()
    return trade_intent


def _seed_price_bar(db: Session, instrument: Instrument, *, close: float) -> PriceBar:
    """A single completed 60s bar for `instrument`, close-only matters for
    the structure-break bar-close confirmation gate -- open/high/low are
    filler values around close."""
    bar = PriceBar(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        timeframe=BAR_TIMEFRAME,
        bucket_start=datetime.now(UTC),
        open=close, high=close + 1, low=close - 1, close=close,
        volume=1000,
    )
    db.add(bar)
    db.flush()
    return bar


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
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
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
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service._raise_if_option_chain_stale",
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


def test_dispatch_raises_when_option_chain_is_stale_for_a_live_dispatch(
    db: Session, broker, workspace, strategy_run, strategy_config, trading_session,
    option_contract, monkeypatch,
):
    """2026-08-26: the option-chain freshness gate moved here from
    `broker_adapter.preflight.run_preflight_checks` (see
    `_raise_if_option_chain_stale`'s own docstring for why) -- this pins the
    same live-only behavior at its new call site: no `OptionChainSnapshot`
    seeded at all means `classify_option_chain` returns DEAD, which must
    still refuse a genuinely live dispatch.
    """
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
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

    with pytest.raises(ConfigurationError, match="dead"):
        dispatch_trade_intent(db, trading_session, trade_intent)


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

    def place_order(self, request):
        """A resolved-FILLED entry order now also triggers protective-stop
        placement (`place_protective_stop`, LIVE-only) — this double must
        answer that call too, not just the entry-side `get_order_status`/
        `peek_cached_order_update` it was originally built for. A plain
        `OPEN` ack is enough; these tests assert on the entry order/
        position/trade_intent, not on the protective stop itself.
        """
        return OrderResult(
            idempotency_key=request.idempotency_key,
            broker_order_id=f"STOP-{request.idempotency_key}",
            status=BrokerOrderStatus.OPEN,
            filled_qty=0,
            avg_fill_price=None,
        )


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

        def place_order(self, request):
            """order_b resolving to FILLED now also triggers protective-
            stop placement (`place_protective_stop`, LIVE-only) — see
            `_FakeOrderStatusBroker.place_order`'s identical comment.
            """
            return OrderResult(
                idempotency_key=request.idempotency_key,
                broker_order_id=f"STOP-{request.idempotency_key}",
                status=BrokerOrderStatus.OPEN,
                filled_qty=0,
                avg_fill_price=None,
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
    imported into this module and asserting it's actually invoked three
    times: once for `Position.entry_slippage` at open (`_open_position_
    from_fill`, via `dispatch_trade_intent`'s synchronous fill), then
    realized_pnl and exit slippage at close. Against the pre-fix code this
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
    assert len(calls) == 3
    # entry_slippage(actual_fill, intended_entry, qty, side) -- argument
    # order deliberately swapped relative to the exit-side calls below (see
    # _open_position_from_fill's own comment): both are Decimal('80.0') in
    # this fixture, i.e. no entry slippage since the mock fills exactly at
    # the requested price.
    assert calls[0] == (Decimal("80.0"), Decimal("80.0"), Decimal("25"), SignalSide.BUY)
    # realized_pnl(entry, exit)
    assert calls[1] == (Decimal("80.0000"), Decimal("72.0"), Decimal("25"), SignalSide.BUY)
    # slippage(intended_trigger, exit)
    assert calls[2] == (72.0, Decimal("72.0"), Decimal("25"), SignalSide.BUY)
    assert position.entry_slippage == 0.0


def test_entry_slippage_sign_is_favorable_positive_unfavorable_negative(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """The zero-slippage case above can't catch an argument-order mistake --
    0 is symmetric regardless of which price comes first. This forces a
    real, nonzero fill-vs-intended gap via `queue_fill_scenario` on both
    sides: every strategy in this codebase only ever enters long (buys an
    option -- no strategy produces a SELL entry signal), so both cases here
    are BUY, varying only whether the fill lands better or worse than
    intended. See `_open_position_from_fill`'s own comment for why the
    argument order is deliberately swapped from the exit-side formula.
    """
    better_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, side=SignalSide.BUY, entry_price=80.0
    )
    broker.queue_fill_scenario(
        option_contract.symbol,
        FillScenario(status=BrokerOrderStatus.FILLED, avg_fill_price=76.0),
    )
    dispatch_trade_intent(db, trading_session, better_intent, broker=broker)
    better_position = db.query(Position).filter(Position.trade_intent_id == better_intent.id).one()
    # Paid 76 instead of the intended 80 -- 4 better per unit, 25 qty --
    # favorable, must be positive.
    assert better_position.entry_slippage == 100.0

    worse_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, side=SignalSide.BUY, entry_price=80.0
    )
    broker.queue_fill_scenario(
        option_contract.symbol,
        FillScenario(status=BrokerOrderStatus.FILLED, avg_fill_price=84.0),
    )
    dispatch_trade_intent(db, trading_session, worse_intent, broker=broker)
    worse_position = db.query(Position).filter(Position.trade_intent_id == worse_intent.id).one()
    # Paid 84 instead of the intended 80 -- 4 worse per unit -- unfavorable,
    # must be negative.
    assert worse_position.entry_slippage == -100.0


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


def test_close_position_from_external_fill_returns_none_when_already_closed(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """2026-09-02 QC follow-up: close_position_from_external_fill now
    acquires LOCK_EXECUTION_SINGLETON and re-checks OPEN status itself
    (after a db.refresh, not a possibly-stale attribute a caller loaded
    earlier) -- exactly close_position's own idempotent-no-op contract,
    proven the same way test_close_position_is_idempotent_when_already_
    closed proves it for close_position. Simulates the real risk this
    closes: _attempt_auto_repair loads a position, then does a
    broker.get_recent_trades() network round-trip before calling this
    function -- a real window for another closer to win the race in.
    """
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    first = close_position(
        db, trading_session, position, ExitReason.MANUAL, intended_price=80.0, broker=broker
    )
    assert first is not None

    fill = TradeFill(
        broker_order_id="SHOONYA-MANUAL-1",
        contract_symbol=option_contract.symbol,
        side=ContractOrderSide.SELL,
        qty=position.qty,
        avg_price=999.0,
        ts=datetime.now(UTC),
    )
    second = close_position_from_external_fill(db, trading_session, position, fill)

    assert second is None
    assert db.query(TradeOutcome).filter(TradeOutcome.position_id == position.id).count() == 1
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


def test_close_position_retries_with_a_new_order_after_a_prior_attempt_was_rejected(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """Live incident 2026-09-02: a cancelled/rejected exit order used to
    permanently strand `close_position` -- the fixed `exit:{position.id}`
    idempotency key made every later call find that same dead order and
    never place a new one. This pins the fix: a *second* call after a
    rejected first attempt must place a genuinely new order (a distinct
    `:retry0`-suffixed idempotency key) rather than re-reporting the same
    dead one, and a normal fill on that retry must actually close the
    position.
    """
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    broker.queue_fill_scenario(
        option_contract.symbol, FillScenario(status=BrokerOrderStatus.REJECTED)
    )
    first = close_position(
        db, trading_session, position, ExitReason.MANUAL, intended_price=80.0, broker=broker
    )
    assert first is None
    db.refresh(position)
    assert position.status == PositionStatus.OPEN

    # No fault scenario queued this time -- the retry should hit the mock's
    # normal always-fills behavior.
    second = close_position(
        db, trading_session, position, ExitReason.MANUAL, intended_price=80.0, broker=broker
    )

    assert second is not None
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED

    exit_orders = (
        db.query(Order)
        .filter(Order.position_id == position.id)
        .order_by(Order.submitted_at)
        .all()
    )
    # The retry's suffix is `len(exit_attempts)` at the time it's placed --
    # 1 prior attempt (the original, unsuffixed `exit:{id}` key) exists when
    # this second call runs, so the retry is `:retry1`, not `:retry0`.
    assert [o.idempotency_key for o in exit_orders] == [
        f"exit:{position.id}",
        f"exit:{position.id}:retry1",
    ]
    assert exit_orders[0].status == OrderStatus.REJECTED
    assert exit_orders[1].status == OrderStatus.FILLED


def test_close_position_gives_up_after_max_exit_order_attempts(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """A genuinely un-exitable position (every attempt rejected) must stop
    placing new live broker orders once `_MAX_EXIT_ORDER_ATTEMPTS` is hit,
    alerting distinctly instead of retrying forever.
    """
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    for _ in range(5):
        broker.queue_fill_scenario(
            option_contract.symbol, FillScenario(status=BrokerOrderStatus.REJECTED)
        )
        outcome = close_position(
            db, trading_session, position, ExitReason.MANUAL, intended_price=80.0, broker=broker
        )
        assert outcome is None

    assert db.query(Order).filter(Order.position_id == position.id).count() == 5

    # 6th call: attempts are exhausted, must not place a 6th order.
    outcome = close_position(
        db, trading_session, position, ExitReason.MANUAL, intended_price=80.0, broker=broker
    )
    assert outcome is None
    assert db.query(Order).filter(Order.position_id == position.id).count() == 5
    db.refresh(position)
    assert position.status == PositionStatus.OPEN

    alert = (
        db.query(SystemAlert)
        .filter(SystemAlert.trading_session_id == trading_session.id)
        .filter(SystemAlert.category == "exit_order_attempts_exhausted")
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
    an explicit broker, on a live_enabled
    session (strategy not force_paper), with `get_execution_broker`/preflight neutralized the same
    way `test_evaluate_trade_intent_margin_check_failed`-style tests
    already do elsewhere in this suite.
    """
    trading_session.mode = SafeMode.LIVE_ENABLED
    trading_session.daily_loss_cap = 1.0  # trivially small so any loss breaches it
    db.add(trading_session)
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
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service._raise_if_option_chain_stale",
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
    trading_session.mode = SafeMode.LIVE_ENABLED
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
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service._raise_if_option_chain_stale",
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
    db: Session,
    trading_session: TradingSession,
    position: Position,
    *,
    broker_order_id: str,
    idempotency_key: str | None = None,
    intended_exit_reason: ExitReason | None = None,
) -> Order:
    now = datetime.now(UTC)
    order = Order(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=position.option_contract_id,
        position_id=position.id,
        idempotency_key=idempotency_key or f"exit:{position.id}",
        mode=OrderMode.LIVE,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        qty=position.qty,
        status=OrderStatus.PENDING,
        filled_qty=0,
        avg_fill_price=None,
        broker_order_id=broker_order_id,
        intended_exit_reason=intended_exit_reason,
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


def test_close_position_records_the_intended_exit_reason_when_not_filled_synchronously(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """2026-08-25 fix: the real reason `close_position` was called for must
    survive onto the `exit:` `Order` row even when the broker doesn't fill
    it synchronously — this is what lets a later
    `reconcile_pending_live_exit_orders` report the true TARGET/TRAIL/etc.
    reason instead of defaulting to the generic RECONCILED. `queue_fill_
    scenario` is the existing fault-injection hook `mock/adapter.py`'s own
    docstring says exists exactly for this: exercising a non-terminal exit
    fill without contriving a real fill price.
    """
    position = _open_live_position(db, broker, trading_session, strategy_run, option_contract)
    broker.queue_fill_scenario(
        option_contract.symbol, FillScenario(status=BrokerOrderStatus.PENDING)
    )

    outcome = close_position(
        db, trading_session, position, ExitReason.TARGET, 95.0, broker=broker
    )

    assert outcome is None  # left OPEN -- the order didn't fill synchronously
    db.refresh(position)
    assert position.status == PositionStatus.OPEN

    exit_order = (
        db.query(Order).filter(Order.idempotency_key == f"exit:{position.id}").one()
    )
    assert exit_order.status != OrderStatus.FILLED
    assert exit_order.intended_exit_reason == ExitReason.TARGET


def test_reconcile_pending_live_exit_orders_uses_the_recorded_intended_reason(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """The real fix: a late-discovered fill for an order that recorded its
    intended reason must report that reason, not the generic RECONCILED —
    and the TRAIL case's own `trail_plan.status` side effect (previously
    unreachable for a late-discovered exit, since it was always mislabeled
    RECONCILED) must fire correctly too.
    """
    position = _open_live_position(db, broker, trading_session, strategy_run, option_contract)
    exit_order = _make_pending_live_exit_order(
        db,
        trading_session,
        position,
        broker_order_id="EXIT-TRAIL-1",
        intended_exit_reason=ExitReason.TRAIL,
    )
    fake_broker = _FakeOrderStatusBroker(
        cached_result=OrderResult(
            idempotency_key=exit_order.idempotency_key,
            broker_order_id=exit_order.broker_order_id,
            status=BrokerOrderStatus.FILLED,
            filled_qty=position.qty,
            avg_fill_price=95.0,
        )
    )

    reconcile_pending_live_exit_orders(
        db, trading_session, allow_rest_fallback=False, broker=fake_broker  # type: ignore[arg-type]
    )

    db.refresh(position)
    assert position.status == PositionStatus.CLOSED

    outcome = db.query(TradeOutcome).filter(TradeOutcome.position_id == position.id).one()
    assert outcome.exit_reason == ExitReason.TRAIL

    trail_plan = db.query(TrailPlan).filter(TrailPlan.position_id == position.id).one()
    assert trail_plan.status == TrailPlanStatus.TRIGGERED


def test_reconcile_pending_live_exit_orders_protective_stop_wins_over_intended_reason(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """Defensive: `is_protective_stop` (from the `stop:` idempotency-key
    prefix) must take priority over `intended_exit_reason` even in this
    contrived combination that shouldn't occur in practice (`_place_
    protective_stop` never sets `intended_exit_reason`) — a resting
    protective stop's own fill is always a genuine STOP exit by
    construction, regardless of what any other field says.
    """
    position = _open_live_position(db, broker, trading_session, strategy_run, option_contract)
    exit_order = _make_pending_live_exit_order(
        db,
        trading_session,
        position,
        broker_order_id="STOP-1",
        idempotency_key=f"stop:{position.id}",
        intended_exit_reason=ExitReason.TARGET,
    )
    fake_broker = _FakeOrderStatusBroker(
        cached_result=OrderResult(
            idempotency_key=exit_order.idempotency_key,
            broker_order_id=exit_order.broker_order_id,
            status=BrokerOrderStatus.FILLED,
            filled_qty=position.qty,
            avg_fill_price=80.0,
        )
    )

    reconcile_pending_live_exit_orders(
        db, trading_session, allow_rest_fallback=False, broker=fake_broker  # type: ignore[arg-type]
    )

    outcome = db.query(TradeOutcome).filter(TradeOutcome.position_id == position.id).one()
    assert outcome.exit_reason == ExitReason.STOP


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


def test_reconcile_pending_live_exit_orders_continues_after_one_orders_resolve_raises(
    db: Session, broker, trading_session, strategy_run, option_contract, monkeypatch
):
    """2026-08-26 fix: `resolve_broker_for_position` raising
    `ConfigurationError` for one position (the real, live-found failure mode
    right after a restart with Shoonya not yet reconnected) must not abort
    the whole reconciliation loop -- a second position whose broker resolves
    fine must still get processed in the same cycle, matching the entry
    side's own `..._continues_after_one_orders_broker_error` test. Before
    this fix, `resolve_broker_for_position` was called with no try/except at
    all, so this scenario would propagate straight out of the function.
    """
    import app.modules.execution_engine.paper.service as paper_service_module

    position_a = _open_live_position(db, broker, trading_session, strategy_run, option_contract)
    exit_order_a = _make_pending_live_exit_order(
        db, trading_session, position_a, broker_order_id="EXIT-CFG-A"
    )
    position_b = _open_live_position(db, broker, trading_session, strategy_run, option_contract)
    exit_order_b = _make_pending_live_exit_order(
        db, trading_session, position_b, broker_order_id="EXIT-CFG-B"
    )

    fake_broker = _FakeOrderStatusBroker(
        cached_result=OrderResult(
            idempotency_key=exit_order_b.idempotency_key,
            broker_order_id=exit_order_b.broker_order_id,
            status=BrokerOrderStatus.FILLED,
            filled_qty=position_b.qty,
            avg_fill_price=90.0,
        )
    )

    def _fake_resolve(db_arg, trading_session_arg, position_arg):
        if position_arg.id == position_a.id:
            raise ConfigurationError("Shoonya not connected")
        return fake_broker

    monkeypatch.setattr(paper_service_module, "resolve_broker_for_position", _fake_resolve)

    reconcile_pending_live_exit_orders(db, trading_session, allow_rest_fallback=False)

    db.refresh(exit_order_a)
    db.refresh(exit_order_b)
    db.refresh(position_a)
    db.refresh(position_b)
    assert exit_order_a.status == OrderStatus.PENDING
    assert position_a.status == PositionStatus.OPEN
    assert exit_order_b.status == OrderStatus.FILLED
    assert position_b.status == PositionStatus.CLOSED


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


class _FakeLiveBrokerWithModify:
    """Controllable LIVE-classified double for the TSL/resting-protective-
    stop tests — full scripted control over `place_order` (synchronous
    entry fill, `OPEN`-not-`FILLED` ack for the `stop:`-tagged protective
    order, matching a real resting stop's own ack shape) and `modify_order`
    (scriptable success/failure) that `MockBrokerAdapter` can't give
    (always fills synchronously; `modify_order` is a no-op stub). Not a
    `MockBrokerAdapter` subclass, so `is_execution_broker_live` reads it as
    LIVE with no extra wrapper needed.
    """

    def __init__(self, entry_fill_price: float) -> None:
        self.entry_fill_price = entry_fill_price
        self.modify_calls: list[dict] = []
        self.modify_raises: Exception | None = None
        self._next_stop_id = 1

    def place_order(self, request: OrderRequest) -> OrderResult:
        if request.idempotency_key.startswith("stop:"):
            broker_order_id = f"STOP-{self._next_stop_id}"
            self._next_stop_id += 1
            return OrderResult(
                idempotency_key=request.idempotency_key,
                broker_order_id=broker_order_id,
                status=BrokerOrderStatus.OPEN,
                filled_qty=0,
                avg_fill_price=None,
            )
        return OrderResult(
            idempotency_key=request.idempotency_key,
            broker_order_id="ENTRY-1",
            status=BrokerOrderStatus.FILLED,
            filled_qty=request.qty,
            avg_fill_price=self.entry_fill_price,
        )

    def modify_order(self, broker_order_id: str, **changes: object) -> OrderResult:
        self.modify_calls.append({"broker_order_id": broker_order_id, **changes})
        if self.modify_raises is not None:
            raise self.modify_raises
        return OrderResult(
            idempotency_key=broker_order_id,
            broker_order_id=broker_order_id,
            status=BrokerOrderStatus.OPEN,
            filled_qty=0,
            avg_fill_price=None,
        )

    def cancel_order(self, broker_order_id: str) -> OrderResult:
        raise AssertionError("cancel_order should not be called in this test")

    def get_positions(self) -> list:
        # dispatch_trade_intent/close_position both run an event-triggered
        # reconciliation pass after every real fill -- an empty book is a
        # harmless, real mismatch (local position exists, broker doesn't)
        # rather than a crash; not what these tests are exercising.
        return []


def test_evaluate_open_position_syncs_resting_stop_as_trail_tightens(
    db: Session, trading_session, strategy_run, option_contract, monkeypatch
):
    """TSL half of "Hard SL with Local Target": once the resting protective
    stop exists (`place_protective_stop`), every trail tightening must
    push a real `ModifyOrder` to keep it in step — this is that happy path.
    """
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.run_preflight_checks",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service._raise_if_option_chain_stale",
        lambda *args, **kwargs: None,
    )
    live_broker = _FakeLiveBrokerWithModify(entry_fill_price=100.0)

    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=100.0, stop_price=90.0, target_price=140.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=live_broker)  # type: ignore[arg-type]
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()
    assert stop_plan.resting_order_id == "STOP-1"
    assert _price(stop_plan.resting_order_price) == pytest.approx(90.0)

    # Activation: trail tightens to lock in 0 gain beyond activation (100 +
    # 50% of the 40-wide range = 120) -- the resting stop should move from
    # its original 90 up to 120.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=120.0, broker=live_broker  # type: ignore[arg-type]
    )
    assert outcome is None
    db.refresh(stop_plan)
    assert len(live_broker.modify_calls) == 1
    assert live_broker.modify_calls[0]["broker_order_id"] == "STOP-1"
    assert live_broker.modify_calls[0]["contract_symbol"] == option_contract.symbol
    assert live_broker.modify_calls[0]["trigger_price"] == pytest.approx(120.0)
    # 2026-09-01: live-confirmed Shoonya rejects ModifyOrder outright
    # ("ORA: no qty field in modify") without qty, regardless of which
    # fields are actually changing -- must be sent on every call.
    assert live_broker.modify_calls[0]["qty"] == position.qty
    assert _price(stop_plan.resting_order_price) == pytest.approx(120.0)

    # Further tightening (126 -> locks in 123) syncs again.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=126.0, broker=live_broker  # type: ignore[arg-type]
    )
    assert outcome is None
    db.refresh(stop_plan)
    assert len(live_broker.modify_calls) == 2
    assert live_broker.modify_calls[1]["trigger_price"] == pytest.approx(123.0)
    assert _price(stop_plan.resting_order_price) == pytest.approx(123.0)


def test_evaluate_open_position_tsl_sync_retries_after_a_rejected_modify(
    db: Session, trading_session, strategy_run, option_contract, monkeypatch
):
    """Fallback for a rejected/failed `ModifyOrder`: the resting stop stays
    armed at its last *confirmed* price (never touched on failure), and the
    next cycle retries automatically since `resting_order_price` still
    disagrees with the locally-tightened level — no separate retry/backoff
    bookkeeping, and no CRITICAL alert (the position isn't unprotected).
    """
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.run_preflight_checks",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service._raise_if_option_chain_stale",
        lambda *args, **kwargs: None,
    )
    live_broker = _FakeLiveBrokerWithModify(entry_fill_price=100.0)

    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=100.0, stop_price=90.0, target_price=140.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=live_broker)  # type: ignore[arg-type]
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()

    live_broker.modify_raises = BrokerError("simulated rejection")
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=120.0, broker=live_broker  # type: ignore[arg-type]
    )
    assert outcome is None
    db.refresh(stop_plan)
    assert len(live_broker.modify_calls) == 1
    # Untouched -- still the resting order and its last *confirmed* price,
    # not the failed 120 attempt.
    assert stop_plan.resting_order_id == "STOP-1"
    assert _price(stop_plan.resting_order_price) == pytest.approx(90.0)
    alert = (
        db.query(SystemAlert)
        .filter(SystemAlert.category == "protective_stop_modify_failed")
        .one()
    )
    assert alert.severity == AlertSeverity.WARNING

    # Same price again (no further real-world movement) -- the trail's own
    # local level already tightened to 120 on the first call, so this only
    # re-attempts the sync (comparing against resting_order_price, not
    # against whether the trail tightened *this* cycle) -- not a fresh
    # tightening event.
    live_broker.modify_raises = None
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=120.0, broker=live_broker  # type: ignore[arg-type]
    )
    assert outcome is None
    db.refresh(stop_plan)
    assert len(live_broker.modify_calls) == 2
    assert _price(stop_plan.resting_order_price) == pytest.approx(120.0)


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


def test_evaluate_open_position_pe_no_structure_break_when_underlying_falls_further(
    db: Session, broker, trading_session, strategy_run, option_contract_pe
):
    """The exact live-confirmed 2026-09-01 bug: a bought PE profits when the
    underlying FALLS, so a falling underlying is this position's own
    favorable direction and must never be treated as a structure breach.
    Before the fix (`favorable = side == SignalSide.BUY`, always True,
    wrongly reused for this underlying-based check), this exact scenario
    exited immediately and incorrectly -- confirmed against 19/19 real
    PE structure_break exits that day, all mathematically false triggers.
    """
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract_pe,
        side=SignalSide.BUY,
        entry_price=80.0, stop_price=72.0, target_price=92.0, structure_level=22000.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    # Underlying falls well below structure_level (22000) -- the PE's own
    # favorable direction, must NOT trigger a structure-break exit.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21000.0
    )

    assert outcome is None
    db.refresh(position)
    assert position.status == PositionStatus.OPEN


def test_evaluate_open_position_pe_exits_on_structure_break_when_underlying_rises(
    db: Session, broker, trading_session, strategy_run, option_contract_pe
):
    """Mirror of the CE case above: a PE's structure is broken when the
    underlying RISES back above the level it broke down through (the
    unfavorable direction for a PE), invalidating the bearish setup."""
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract_pe,
        side=SignalSide.BUY,
        entry_price=80.0, stop_price=72.0, target_price=92.0, structure_level=22000.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=22010.0
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


def test_evaluate_open_position_breach_within_buffer_does_not_start_a_candidate(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """A breach that doesn't clear the buffer isn't noise-confirmation
    material at all — no candidate should even start, distinct from a
    breach that starts a candidate but doesn't yet persist.
    """
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0, structure_level=22000.0,
        structure_break_buffer=20.0, structure_break_persistence_seconds=6.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()

    # 21990 is below structure_level (22000) but within the 20-point buffer
    # (buffered_level = 21980) — not a breach at all.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21990.0
    )

    assert outcome is None
    db.refresh(stop_plan)
    assert stop_plan.structure_break_candidate_since is None


def test_evaluate_open_position_noisy_breach_then_reclaim_stays_open(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """The exact bug scenario this fix targets: a single tick pierces the
    buffered level, then the very next tick reclaims it — must not exit,
    and the candidate state must clear so it doesn't silently confirm on a
    later, unrelated breach.
    """
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0, structure_level=22000.0,
        structure_break_buffer=20.0, structure_break_persistence_seconds=6.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()

    # 21975 is beyond the buffered level (21980) — a genuine candidate breach.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21975.0
    )
    assert outcome is None
    db.refresh(stop_plan)
    assert stop_plan.structure_break_candidate_since is not None
    assert _price(stop_plan.structure_break_candidate_extreme) == pytest.approx(21975.0)

    # Reclaims back above the buffered level on the very next tick.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21995.0
    )
    assert outcome is None
    db.refresh(position)
    assert position.status == PositionStatus.OPEN
    db.refresh(stop_plan)
    assert stop_plan.structure_break_candidate_since is None
    assert stop_plan.structure_break_candidate_extreme is None


def test_evaluate_open_position_confirms_structure_break_once_persisted(
    db: Session, broker, trading_session, strategy_run, option_contract, instrument
):
    """A breach that clears the buffer, holds past the persistence window,
    AND is confirmed by the latest completed bar's own close (not just a
    live tick) confirms as a real STRUCTURE_BREAK exit.
    """
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0, structure_level=22000.0,
        structure_break_buffer=20.0, structure_break_persistence_seconds=6.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()

    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21975.0
    )
    assert outcome is None
    db.refresh(stop_plan)
    assert stop_plan.structure_break_candidate_since is not None

    # Simulate the persistence window having elapsed since the first
    # breaching tick, rather than sleeping in the test.
    stop_plan.structure_break_candidate_since -= timedelta(seconds=7)
    db.add(stop_plan)
    db.flush()

    # The latest completed bar also closed beyond the buffered level
    # (21980) -- real bar evidence, not just a live tick.
    _seed_price_bar(db, instrument, close=21970.0)

    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21975.0
    )

    assert outcome is not None
    assert outcome.exit_reason == ExitReason.STRUCTURE_BREAK
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED


def test_evaluate_open_position_does_not_confirm_structure_break_without_a_bar_close(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """Persistence elapsed on a live-tick breach, but no completed bar
    exists yet to confirm it actually closed beyond the level -- the exact
    live-observed gap this gate closes (2026-08-24: a 120s-persistence
    trade still confirmed on tick noise alone). Must stay a candidate, not
    exit, until real bar evidence exists.
    """
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0, structure_level=22000.0,
        structure_break_buffer=20.0, structure_break_persistence_seconds=6.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()

    evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21975.0
    )
    db.refresh(stop_plan)
    assert stop_plan.structure_break_candidate_since is not None
    stop_plan.structure_break_candidate_since -= timedelta(seconds=7)
    db.add(stop_plan)
    db.flush()

    # No PriceBar seeded at all -- fail-safe: no bar evidence means no confirm.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21975.0
    )

    assert outcome is None
    db.refresh(position)
    assert position.status == PositionStatus.OPEN
    db.refresh(stop_plan)
    assert stop_plan.structure_break_candidate_since is not None  # still an active candidate


def test_evaluate_open_position_does_not_confirm_structure_break_if_bar_recovered(
    db: Session, broker, trading_session, strategy_run, option_contract, instrument
):
    """The core new-behavior case: persistence elapsed on a live-tick
    breach, but the latest COMPLETED bar closed back inside the buffered
    level (price recovered by bar-close time even though a tick dipped
    below mid-bar) -- must not confirm, must stay a candidate.
    """
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0, structure_level=22000.0,
        structure_break_buffer=20.0, structure_break_persistence_seconds=6.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()

    evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21975.0
    )
    db.refresh(stop_plan)
    assert stop_plan.structure_break_candidate_since is not None
    stop_plan.structure_break_candidate_since -= timedelta(seconds=7)
    db.add(stop_plan)
    db.flush()

    # Buffered level is 21980 (22000 - 20 buffer) -- this bar's close
    # (21985) is back INSIDE it, even though the live tick dipped to 21975.
    _seed_price_bar(db, instrument, close=21985.0)

    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21975.0
    )

    assert outcome is None
    db.refresh(position)
    assert position.status == PositionStatus.OPEN
    db.refresh(stop_plan)
    assert stop_plan.structure_break_candidate_since is not None  # still an active candidate


def test_evaluate_open_position_zero_persistence_skips_bar_close_gate(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """Backward-compat guard: a strategy that never opted into persistence
    (unset, defaults to 0) must keep the exact original single-tick
    instant-exit behavior -- the new bar-close gate must not apply here,
    even with zero PriceBar rows seeded.
    """
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0, structure_level=22000.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=82.0, broker=broker, underlying_price=21990.0
    )

    assert outcome is not None
    assert outcome.exit_reason == ExitReason.STRUCTURE_BREAK


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
