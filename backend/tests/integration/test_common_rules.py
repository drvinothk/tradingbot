"""Shared strategy_engine.common_rules helpers — the guards every Phase 4
confirmation-filter strategy (ORB, VWAP Pullback, EMA Micro-pullback) relies
on instead of duplicating: no-signal-while-in-position, full-candle
completion, and the `since`/`until`/`limit` bar-fetching shapes each real
strategy needs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy.orm import Session

from app.domain.execution.models import (
    Order,
    OrderMode,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
)
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import (
    IndicatorSnapshot,
    Instrument,
    OptionContract,
    OptionType,
    PriceBar,
)
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
from app.modules.strategy_engine.common_rules import (
    BAR_TIMEFRAME,
    ConfirmationFilterStrategy,
    _parse_hhmm,
    compute_stop_target,
    get_open_position_for_run,
    get_recent_completed_bars,
    get_recent_indicator_values,
)


class TestComputeStopTarget:
    """2026-08-12 regression coverage for a real live bug: plain
    round(price, 2) produces prices like 132.84 that aren't a multiple of a
    real instrument's tick size, which risk_engine's tick-alignment check
    then correctly rejects -- never surfaced against the mock adapter's
    clean whole-number synthetic premiums, only once real, genuinely
    fractional Shoonya-sourced premiums flowed into signal generation.
    """

    def test_rounds_to_the_instrument_tick_size(self):
        stop, target = compute_stop_target(147.6, 0.10, 0.15, tick_size=0.05)
        assert stop == 132.85  # 147.6 * 0.9 = 132.84 -- not tick-aligned, rounds to 132.85
        assert target == 169.75  # 147.6 * 1.15 = 169.74 -- rounds to 169.75

        # Confirm both are exact multiples of the tick size, not just
        # visually rounded -- this is what risk_engine._is_tick_aligned
        # actually checks (Decimal(str(price)) % Decimal(str(tick_size))).
        from decimal import Decimal

        assert Decimal(str(stop)) % Decimal("0.05") == 0
        assert Decimal(str(target)) % Decimal("0.05") == 0

    def test_zero_tick_size_falls_back_to_plain_rounding(self):
        """No real tick size to hand (defensive default, see compute_stop_
        target's own docstring) -- must not crash, old 2-decimal behavior."""
        stop, target = compute_stop_target(147.6, 0.10, 0.15, tick_size=0.0)
        assert stop == 132.84
        assert target == 169.74

    def test_already_aligned_price_is_unchanged(self):
        stop, target = compute_stop_target(100.0, 0.10, 0.15, tick_size=0.05)
        assert stop == 90.0
        assert target == 115.0


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="common-rules-test-account",
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
        expiry_date="2026-07-30",
        strike=22000,
        option_type=OptionType.CE,
        symbol="NIFTY26JUL22000CE",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="common-rules-test", strategy_type="orb"
    )
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


def _make_trade_intent(db: Session, trading_session, strategy_run, option_contract) -> TradeIntent:
    signal = Signal(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        strategy_config_id=strategy_run.strategy_config_id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        side=SignalSide.BUY,
        entry_price=80.0,
        stop_price=72.0,
        target_price=92.0,
        qty_lots=1,
        generated_at=datetime.now(UTC),
    )
    db.add(signal)
    db.flush()

    intent = TradeIntent(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        signal_id=signal.id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        idempotency_key=f"test:{uuid.uuid4()}",
        side=SignalSide.BUY,
        qty_lots=1,
        entry_price=80.0,
        stop_price=72.0,
        target_price=92.0,
        status=TradeIntentStatus.DISPATCHED,
        created_at=datetime.now(UTC),
        dispatched_at=datetime.now(UTC),
    )
    db.add(intent)
    db.flush()
    return intent


def _make_position(
    db: Session, trading_session, option_contract, trade_intent, *, status: PositionStatus
) -> Position:
    now = datetime.now(UTC)
    order = Order(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=trade_intent.id,
        idempotency_key=trade_intent.idempotency_key,
        mode=OrderMode.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=25,
        status=OrderStatus.FILLED,
        filled_qty=25,
        avg_fill_price=80.0,
        submitted_at=now,
        updated_at=now,
    )
    db.add(order)
    db.flush()

    position = Position(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=trade_intent.id,
        opening_order_id=order.id,
        side=OrderSide.BUY,
        qty=25,
        entry_price=80.0,
        status=status,
        opened_at=now,
        closed_at=None if status == PositionStatus.OPEN else now,
    )
    db.add(position)
    db.flush()
    return position


class TestGetOpenPositionForRun:
    def test_no_position_returns_none(self, db: Session, strategy_run):
        assert get_open_position_for_run(db, strategy_run) is None

    def test_open_position_is_found(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
        position = _make_position(
            db, trading_session, option_contract, intent, status=PositionStatus.OPEN
        )
        found = get_open_position_for_run(db, strategy_run)
        assert found is not None
        assert found.id == position.id

    def test_closed_position_is_not_found(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
        _make_position(db, trading_session, option_contract, intent, status=PositionStatus.CLOSED)
        assert get_open_position_for_run(db, strategy_run) is None


def _seed_bars(
    db: Session, instrument: Instrument, *, count: int, start: datetime
) -> list[PriceBar]:
    bars = []
    for i in range(count):
        bar = PriceBar(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            timeframe=BAR_TIMEFRAME,
            bucket_start=start + timedelta(minutes=i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=10,
        )
        db.add(bar)
        bars.append(bar)
    db.flush()
    return bars


class TestGetRecentCompletedBars:
    def test_returns_ascending_order(self, db: Session, instrument: Instrument):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        _seed_bars(db, instrument, count=5, start=start)

        bars = get_recent_completed_bars(db, instrument.id, BAR_TIMEFRAME)
        assert len(bars) == 5
        assert [b.bucket_start for b in bars] == sorted(b.bucket_start for b in bars)

    def test_limit_returns_the_most_recent_n(self, db: Session, instrument: Instrument):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        seeded = _seed_bars(db, instrument, count=5, start=start)

        bars = get_recent_completed_bars(db, instrument.id, BAR_TIMEFRAME, limit=2)
        assert [b.id for b in bars] == [seeded[3].id, seeded[4].id]

    def test_since_until_bounds_a_fixed_window(self, db: Session, instrument: Instrument):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        _seed_bars(db, instrument, count=10, start=start)

        window = get_recent_completed_bars(
            db,
            instrument.id,
            BAR_TIMEFRAME,
            since=start + timedelta(minutes=2),
            until=start + timedelta(minutes=5),
        )
        assert [b.bucket_start for b in window] == [
            start + timedelta(minutes=2),
            start + timedelta(minutes=3),
            start + timedelta(minutes=4),
        ]

    def test_different_instrument_is_excluded(self, db: Session, instrument: Instrument):
        other = Instrument(
            id=uuid.uuid4(), symbol="BANKNIFTY", exchange="NFO", lot_size=15, tick_size=0.05
        )
        db.add(other)
        db.flush()
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        _seed_bars(db, instrument, count=1, start=start)
        _seed_bars(db, other, count=1, start=start)

        bars = get_recent_completed_bars(db, instrument.id, BAR_TIMEFRAME)
        assert len(bars) == 1


class _RecordingStrategy(ConfirmationFilterStrategy):
    def __init__(self, instrument_id: uuid.UUID) -> None:
        super().__init__(instrument_id)
        self.calls: list[PriceBar] = []

    def check_setup(self, db, strategy_run, latest_bar):
        self.calls.append(latest_bar)
        return None


class TestConfirmationFilterStrategy:
    def test_no_bars_yet_skips_check_setup(self, db: Session, instrument, strategy_run):
        strategy = _RecordingStrategy(instrument.id)
        assert strategy.evaluate(db, strategy_run) is None
        assert strategy.calls == []

    def test_check_setup_fires_once_per_new_completed_bar(
        self, db: Session, instrument, strategy_run
    ):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        _seed_bars(db, instrument, count=1, start=start)

        strategy = _RecordingStrategy(instrument.id)
        strategy.evaluate(db, strategy_run)
        assert len(strategy.calls) == 1

        # Same latest bar, no new bar completed yet — must not re-fire.
        strategy.evaluate(db, strategy_run)
        assert len(strategy.calls) == 1

        # A new completed bar arrives — fires again.
        _seed_bars(db, instrument, count=1, start=start + timedelta(minutes=1))
        strategy.evaluate(db, strategy_run)
        assert len(strategy.calls) == 2

    def test_skips_entirely_while_run_has_an_open_position(
        self, db: Session, instrument, trading_session, option_contract, strategy_run
    ):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        _seed_bars(db, instrument, count=1, start=start)
        intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
        _make_position(db, trading_session, option_contract, intent, status=PositionStatus.OPEN)

        strategy = _RecordingStrategy(instrument.id)
        result = strategy.evaluate(db, strategy_run)

        assert result is None
        assert strategy.calls == []


def _seed_indicator_values(
    db: Session, instrument: Instrument, indicator_name: str, *,
    values: list[float], start: datetime,
) -> None:
    for i, value in enumerate(values):
        db.add(IndicatorSnapshot(
            id=uuid.uuid4(), instrument_id=instrument.id, indicator_name=indicator_name,
            timeframe=BAR_TIMEFRAME, value=value, ts=start + timedelta(minutes=i),
        ))
    db.flush()


class TestGetRecentIndicatorValues:
    def test_returns_ascending_order(self, db: Session, instrument: Instrument):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        _seed_indicator_values(db, instrument, "EMA9", values=[1.0, 2.0, 3.0], start=start)

        values = get_recent_indicator_values(db, instrument.id, "EMA9", BAR_TIMEFRAME)
        assert values == [1.0, 2.0, 3.0]

    def test_limit_returns_the_most_recent_n(self, db: Session, instrument: Instrument):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        _seed_indicator_values(
            db, instrument, "EMA9", values=[1.0, 2.0, 3.0, 4.0, 5.0], start=start
        )

        values = get_recent_indicator_values(db, instrument.id, "EMA9", BAR_TIMEFRAME, limit=2)
        assert values == [4.0, 5.0]

    def test_since_until_bounds_a_fixed_window(self, db: Session, instrument: Instrument):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        _seed_indicator_values(
            db, instrument, "EMA9", values=[1.0, 2.0, 3.0, 4.0, 5.0], start=start
        )

        values = get_recent_indicator_values(
            db, instrument.id, "EMA9", BAR_TIMEFRAME,
            since=start + timedelta(minutes=1), until=start + timedelta(minutes=3),
        )
        assert values == [2.0, 3.0]

    def test_different_indicator_name_is_excluded(self, db: Session, instrument: Instrument):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        _seed_indicator_values(db, instrument, "EMA9", values=[1.0], start=start)
        _seed_indicator_values(db, instrument, "EMA20", values=[2.0], start=start)

        values = get_recent_indicator_values(db, instrument.id, "EMA9", BAR_TIMEFRAME)
        assert values == [1.0]

    def test_different_instrument_is_excluded(self, db: Session, instrument: Instrument):
        other = Instrument(
            id=uuid.uuid4(), symbol="BANKNIFTY", exchange="NFO", lot_size=15, tick_size=0.05
        )
        db.add(other)
        db.flush()
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        _seed_indicator_values(db, instrument, "EMA9", values=[1.0], start=start)
        _seed_indicator_values(db, other, "EMA9", values=[2.0], start=start)

        values = get_recent_indicator_values(db, instrument.id, "EMA9", BAR_TIMEFRAME)
        assert values == [1.0]


class TestParseHHMM:
    def test_parses_valid_time_string(self):
        assert _parse_hhmm("09:31") == time(9, 31)
        assert _parse_hhmm("15:09") == time(15, 9)

    def test_rejects_malformed_input(self):
        with pytest.raises(ValueError):
            _parse_hhmm("not-a-time")


class TestLogOnce:
    def test_logs_the_first_occurrence(self, caplog: pytest.LogCaptureFixture):
        strategy = _RecordingStrategy(uuid.uuid4())
        logger = logging.getLogger("test.log_once")
        with caplog.at_level(logging.INFO, logger="test.log_once"):
            strategy._log_once(logger, "key", "hello %s", "world")  # noqa: SLF001
        assert caplog.messages == ["hello world"]

    def test_does_not_repeat_for_the_same_key(self, caplog: pytest.LogCaptureFixture):
        strategy = _RecordingStrategy(uuid.uuid4())
        logger = logging.getLogger("test.log_once")
        with caplog.at_level(logging.INFO, logger="test.log_once"):
            strategy._log_once(logger, "key", "first")  # noqa: SLF001
            strategy._log_once(logger, "key", "second")  # noqa: SLF001
        assert caplog.messages == ["first"]

    def test_independent_keys_each_log_their_own_first_occurrence(
        self, caplog: pytest.LogCaptureFixture
    ):
        strategy = _RecordingStrategy(uuid.uuid4())
        logger = logging.getLogger("test.log_once")
        with caplog.at_level(logging.INFO, logger="test.log_once"):
            strategy._log_once(logger, "a", "message a")  # noqa: SLF001
            strategy._log_once(logger, "b", "message b")  # noqa: SLF001
            strategy._log_once(logger, "a", "message a again")  # noqa: SLF001
        assert caplog.messages == ["message a", "message b"]
