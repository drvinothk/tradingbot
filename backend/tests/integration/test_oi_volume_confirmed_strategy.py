"""OIVolumeConfirmedStrategy.check_setup — pure entry-logic tests against
constructed PriceBar/OptionChainSnapshot fixtures. Same "test the setup logic
in isolation" split test_orb_strategy.py uses.
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
from app.modules.strategy_engine.strategies.oi_volume_confirmed import OIVolumeConfirmedStrategy

EXPIRY = date(2026, 7, 30)
LOOKBACK_BARS = 5


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="oivol-test-account",
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
        symbol="NIFTY26JUL22000CE-OIVOL",
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
        symbol="NIFTY26JUL22000PE-OIVOL",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="oivol-test",
        strategy_type="oi_volume_confirmed",
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
    oi: int = 20000,
    volume: int = 5000,
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
                    "volume": volume,
                    "oi": oi,
                },
                {
                    "contract_symbol": option_contract_pe.symbol,
                    "strike": float(option_contract_pe.strike),
                    "option_type": OptionType.PE.value,
                    "ltp": pe_ltp,
                    "bid": pe_ltp - 0.5,
                    "ask": pe_ltp + 0.5,
                    "volume": volume,
                    "oi": oi,
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


def _seed_window(
    db: Session, instrument: Instrument, start: datetime, *, window_high: float, window_low: float
) -> None:
    """LOOKBACK_BARS flat bars, extremes hit at least once, immediately
    before the bar-under-test."""
    for i in range(LOOKBACK_BARS):
        mid = (window_high + window_low) / 2
        high = window_high if i == 0 else mid + 5
        low = window_low if i == 1 else mid - 5
        _seed_bar(
            db, instrument, start + timedelta(minutes=i), open=mid, high=high, low=low, close=mid
        )


class TestOIVolumeConfirmedStrategy:
    def test_no_signal_with_insufficient_bars(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        bar = _seed_bar(db, instrument, start, open=22000, high=22010, low=21990, close=22005)

        assert strategy.check_setup(db, strategy_run, bar) is None

    def test_no_breakout_when_price_stays_within_window(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window(db, instrument, start, window_high=22050.0, window_low=21950.0)

        inside_bar = _seed_bar(
            db, instrument, start + timedelta(minutes=LOOKBACK_BARS),
            open=22000, high=22020, low=21980, close=22010,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, inside_bar) is None

    def test_bullish_breakout_fires_buy_ce(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window(db, instrument, start, window_high=22050.0, window_low=21950.0)

        breakout_bar = _seed_bar(
            db, instrument, start + timedelta(minutes=LOOKBACK_BARS),
            open=22050, high=22080, low=22045, close=22070,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.structure_level == pytest.approx(21950.0)
        assert proposal.stop_price < proposal.entry_price < proposal.target_price

    def test_bearish_breakout_fires_buy_pe(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window(db, instrument, start, window_high=22050.0, window_low=21950.0)

        breakout_bar = _seed_bar(
            db, instrument, start + timedelta(minutes=LOOKBACK_BARS),
            open=21950, high=21955, low=21900, close=21910,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_pe.id
        assert proposal.structure_level == pytest.approx(22050.0)

    def test_breakout_direction_only_fires_once(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window(db, instrument, start, window_high=22050.0, window_low=21950.0)

        breakout_bar = _seed_bar(
            db, instrument, start + timedelta(minutes=LOOKBACK_BARS),
            open=22050, high=22080, low=22045, close=22070,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        first = strategy.check_setup(db, strategy_run, breakout_bar)
        second = strategy.check_setup(db, strategy_run, breakout_bar)

        assert first is not None
        assert second is None

    def test_breakout_blocked_when_participation_below_floor(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        """The chain-participation-weighted ranking config's min_oi/min_volume
        floor (5000/500) excludes the top candidate outright, even though the
        price action itself would otherwise fire — proving the "confirmed"
        half of OI/Volume Confirmed actually gates the trade, not just scores
        it lower.
        """
        start = datetime(2026, 7, 24, 9, 15, tzinfo=UTC)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, start)
        _seed_chain(
            db, instrument, option_contract_ce, option_contract_pe, oi=1000, volume=100
        )
        _seed_window(db, instrument, start, window_high=22050.0, window_low=21950.0)

        breakout_bar = _seed_bar(
            db, instrument, start + timedelta(minutes=LOOKBACK_BARS),
            open=22050, high=22080, low=22045, close=22070,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None
