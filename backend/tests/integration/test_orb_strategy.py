"""ORBStrategy.check_setup — pure entry-logic tests against constructed
PriceBar/OptionChainSnapshot fixtures. Exercises `check_setup` directly
(bypassing `evaluate`'s open-position/bar-dedup guards, which
`test_common_rules.py` already covers generically for every
`ConfirmationFilterStrategy`), same "test the setup logic in isolation"
split `test_synthetic_strategy.py` uses for `SyntheticStrategy.evaluate`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import (
    Instrument,
    OptionChainSnapshot,
    OptionContract,
    OptionType,
    PriceBar,
)
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import ExecutionMode, StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.strategy_engine.common_rules import BAR_TIMEFRAME
from app.modules.strategy_engine.strategies.orb import ORBStrategy

EXPIRY = date(2026, 7, 30)
OR_MINUTES = 15


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="orb-test-account",
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
def option_contract_ce(db: Session, instrument: Instrument) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
        strike=22000,
        option_type=OptionType.CE,
        symbol="NIFTY26JUL22000CE-ORB",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def option_contract_pe(db: Session, instrument: Instrument) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
        strike=22000,
        option_type=OptionType.PE,
        symbol="NIFTY26JUL22000PE-ORB",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="orb-test", strategy_type="orb"
    )
    db.add(config)
    db.flush()
    return config


def _make_strategy_run(
    db: Session, strategy_config: StrategyConfig, trading_session, user: User, started_at: datetime
) -> StrategyRun:
    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=started_at,
        started_by_user_id=user.id,
    )
    db.add(run)
    db.flush()
    return run


def _seed_chain(
    db: Session,
    instrument: Instrument,
    option_contract_ce: OptionContract,
    option_contract_pe: OptionContract,
    *,
    spot: float = 22000.0,
    ce_ltp: float = 80.0,
    pe_ltp: float = 75.0,
) -> None:
    from app.domain.market.models import QuoteTick as QuoteTickRow

    now = datetime.now(UTC)
    db.add(
        QuoteTickRow(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            ltp=spot,
            bid=spot - 1,
            ask=spot + 1,
            volume=10000,
            oi=None,
            ts=now,
        )
    )
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=EXPIRY,
            ts=now,
            chain_data=[
                {
                    "contract_symbol": option_contract_ce.symbol,
                    "strike": float(option_contract_ce.strike),
                    "option_type": OptionType.CE.value,
                    "ltp": ce_ltp,
                    "bid": ce_ltp - 0.5,
                    "ask": ce_ltp + 0.5,
                    "volume": 5000,
                    "oi": 20000,
                },
                {
                    "contract_symbol": option_contract_pe.symbol,
                    "strike": float(option_contract_pe.strike),
                    "option_type": OptionType.PE.value,
                    "ltp": pe_ltp,
                    "bid": pe_ltp - 0.5,
                    "ask": pe_ltp + 0.5,
                    "volume": 5000,
                    "oi": 20000,
                },
            ],
        )
    )
    db.flush()


def _seed_bar(
    db: Session,
    instrument: Instrument,
    bucket_start: datetime,
    *,
    open: float,  # noqa: A002
    high: float,
    low: float,
    close: float,
    volume: int = 1000,
) -> PriceBar:
    bar = PriceBar(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        timeframe=BAR_TIMEFRAME,
        bucket_start=bucket_start,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )
    db.add(bar)
    db.flush()
    return bar


def _seed_opening_range(
    db: Session, instrument: Instrument, or_start: datetime, *, or_high: float, or_low: float
) -> None:
    """A flat opening range: every bar in [or_start, or_start+OR_MINUTES)
    stays within [or_low, or_high], with the extremes hit at least once."""
    _seed_bar(
        db, instrument, or_start,
        open=(or_high + or_low) / 2, high=or_high, low=or_low, close=(or_high + or_low) / 2,
    )
    for i in range(1, OR_MINUTES):
        mid = (or_high + or_low) / 2
        _seed_bar(
            db, instrument, or_start + timedelta(minutes=i),
            open=mid, high=mid + 5, low=mid - 5, close=mid,
        )


class TestORBStrategy:
    def test_no_signal_within_opening_range_window(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        # A bar still inside the OR window (5 minutes in).
        bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=5),
            open=22000, high=22010, low=21990, close=22005,
        )

        assert strategy.check_setup(db, strategy_run, bar) is None

    def test_bullish_breakout_fires_buy_ce(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, or_start, or_high=22050.0, or_low=21950.0)

        breakout_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=22050, high=22080, low=22045, close=22070,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.structure_level == pytest.approx(21950.0)
        assert proposal.stop_price < proposal.entry_price < proposal.target_price

    def test_bearish_breakout_fires_buy_pe(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, or_start, or_high=22050.0, or_low=21950.0)

        breakout_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=21950, high=21955, low=21900, close=21910,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_pe.id
        assert proposal.structure_level == pytest.approx(22050.0)

    def test_no_breakout_when_price_stays_within_range(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, or_start, or_high=22050.0, or_low=21950.0)

        inside_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=22000, high=22020, low=21980, close=22010,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        assert strategy.check_setup(db, strategy_run, inside_bar) is None

    def test_breakout_direction_only_fires_once(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, or_start, or_high=22050.0, or_low=21950.0)

        breakout_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=22050, high=22080, low=22045, close=22070,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        first = strategy.check_setup(db, strategy_run, breakout_bar)
        second = strategy.check_setup(db, strategy_run, breakout_bar)

        assert first is not None
        assert second is None
