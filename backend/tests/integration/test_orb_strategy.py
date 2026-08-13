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

from app.core.clock import IST
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
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
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
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        # Width 40 -- inside ORBStrategy's default NIFTY range filter (20-80).
        _seed_opening_range(db, instrument, or_start, or_high=22030.0, or_low=21990.0)

        breakout_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=22030, high=22060, low=22025, close=22050,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.structure_level == pytest.approx(21990.0)
        assert proposal.stop_price < proposal.entry_price < proposal.target_price

    def test_bearish_breakout_fires_buy_pe(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        # Width 40 -- inside ORBStrategy's default NIFTY range filter (20-80).
        _seed_opening_range(db, instrument, or_start, or_high=22030.0, or_low=21990.0)

        breakout_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=21990, high=21995, low=21950, close=21960,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_pe.id
        assert proposal.structure_level == pytest.approx(22030.0)

    def test_no_breakout_when_price_stays_within_range(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        # Width 40 -- inside ORBStrategy's default NIFTY range filter (20-80).
        _seed_opening_range(db, instrument, or_start, or_high=22030.0, or_low=21990.0)

        inside_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=22000, high=22020, low=21995, close=22010,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        assert strategy.check_setup(db, strategy_run, inside_bar) is None

    def test_breakout_direction_only_fires_once(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        # Width 40 -- inside ORBStrategy's default NIFTY range filter (20-80).
        _seed_opening_range(db, instrument, or_start, or_high=22030.0, or_low=21990.0)

        breakout_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=22030, high=22060, low=22025, close=22050,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        first = strategy.check_setup(db, strategy_run, breakout_bar)
        second = strategy.check_setup(db, strategy_run, breakout_bar)

        assert first is not None
        assert second is None

    def test_opening_range_is_anchored_to_9_15_ist_not_run_start(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        """The strategy run starts well after the real opening range (10:30
        IST) -- the range must still be computed from the fixed 9:15-9:30
        IST window, not from `strategy_run.started_at`, proving the anchor
        is restart-independent."""
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        late_started_at = datetime(2026, 7, 24, 10, 30, tzinfo=IST)
        strategy_run = _make_strategy_run(
            db, strategy_config, trading_session, user, late_started_at
        )
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        # Width 40 -- inside ORBStrategy's default NIFTY range filter (20-80).
        _seed_opening_range(db, instrument, or_start, or_high=22030.0, or_low=21990.0)

        breakout_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=22030, high=22060, low=22025, close=22050,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id

    def test_no_breakout_when_opening_range_window_has_a_gap(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        """Only a handful of the 9:15-9:30 bars are present (e.g. the WS
        feed didn't warm up in time) -- the range isn't well-defined, so no
        breakout should fire even though the sparse bars alone would look
        like one."""
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)

        # Only 3 of the expected 15 opening-range bars are present.
        for i in range(3):
            _seed_bar(
                db, instrument, or_start + timedelta(minutes=i),
                open=22000, high=22010, low=21990, close=22000,
            )

        breakout_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=22050, high=22080, low=22045, close=22070,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

    def test_breakout_blocked_after_entry_cutoff_time(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        """Default cutoff is 10:15 IST -- a breakout bar arriving after that
        (even a genuinely valid one) must not fire."""
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, or_start, or_high=22030.0, or_low=21990.0)

        late_bar = _seed_bar(
            db, instrument, datetime(2026, 7, 24, 10, 16, tzinfo=IST),
            open=22030, high=22060, low=22025, close=22050,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        assert strategy.check_setup(db, strategy_run, late_bar) is None

    def test_breakout_still_fires_right_before_entry_cutoff_time(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, or_start, or_high=22030.0, or_low=21990.0)

        bar = _seed_bar(
            db, instrument, datetime(2026, 7, 24, 10, 14, tzinfo=IST),
            open=22030, high=22060, low=22025, close=22050,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        assert strategy.check_setup(db, strategy_run, bar) is not None

    def test_custom_entry_cutoff_time_is_respected(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        """A configured (non-default) cutoff is what actually gets enforced,
        not just the "10:15" default."""
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, or_start, or_high=22030.0, or_low=21990.0)

        bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=22030, high=22060, low=22025, close=22050,
        )

        strategy = ORBStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, orb_entry_cutoff_time="09:29",
        )
        assert strategy.check_setup(db, strategy_run, bar) is None

    def test_no_breakout_when_opening_range_too_narrow(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        """Range width 10 is below the NIFTY default min (20 points) -- the
        whole day should be skipped, even though the bar itself would
        otherwise look like a valid breakout."""
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, or_start, or_high=22005.0, or_low=21995.0)

        breakout_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=22005, high=22020, low=22000, close=22015,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

    def test_no_breakout_when_opening_range_too_wide(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        """Range width 200 is above the NIFTY default max (80 points) -- an
        already-trending/gap day this strategy shouldn't chase."""
        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, or_start, or_high=22100.0, or_low=21900.0)

        breakout_bar = _seed_bar(
            db, instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=22100, high=22160, low=22095, close=22150,
        )

        strategy = ORBStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

    def test_range_filter_uses_banknifty_thresholds_for_banknifty_instrument(
        self, db: Session, trading_session, strategy_config, user,
    ):
        """A 200-point range would fail NIFTY's default max (80) but is well
        within BANKNIFTY's default band (75-250) -- proves thresholds are
        picked by the instrument's own symbol, not hardcoded to NIFTY."""
        bn_instrument = Instrument(
            id=uuid.uuid4(), symbol="BANKNIFTY", exchange="NFO", lot_size=15, tick_size=0.05,
        )
        db.add(bn_instrument)
        db.flush()
        ce = OptionContract(
            id=uuid.uuid4(), instrument_id=bn_instrument.id, expiry_date=EXPIRY,
            strike=48000, option_type=OptionType.CE, symbol="BANKNIFTY26JUL48000CE-ORB",
        )
        pe = OptionContract(
            id=uuid.uuid4(), instrument_id=bn_instrument.id, expiry_date=EXPIRY,
            strike=48000, option_type=OptionType.PE, symbol="BANKNIFTY26JUL48000PE-ORB",
        )
        db.add_all([ce, pe])
        db.flush()
        _seed_chain(db, bn_instrument, ce, pe, spot=48000.0)

        or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
        strategy_run = _make_strategy_run(db, strategy_config, trading_session, user, or_start)
        _seed_opening_range(db, bn_instrument, or_start, or_high=48100.0, or_low=47900.0)

        breakout_bar = _seed_bar(
            db, bn_instrument, or_start + timedelta(minutes=OR_MINUTES),
            open=48100, high=48160, low=48095, close=48150,
        )

        strategy = ORBStrategy(bn_instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == ce.id
