"""EMAMicroPullbackStrategy.check_setup — pure entry-logic tests against
constructed PriceBar/IndicatorSnapshot/OptionChainSnapshot fixtures, same
split as test_orb_strategy.py / test_vwap_pullback_strategy.py.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.clock import IST
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import (
    IndicatorSnapshot,
    Instrument,
    OptionChainSnapshot,
    OptionContract,
    OptionType,
    PriceBar,
)
from app.domain.market.models import QuoteTick as QuoteTickRow
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import ExecutionMode, StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.strategy_engine.common_rules import BAR_TIMEFRAME
from app.modules.strategy_engine.strategies.ema_micro_pullback import (
    BODY_RATIO_LOOKBACK_BARS,
    EMAMicroPullbackStrategy,
)

EXPIRY = date(2026, 7, 30)
EMA20_BULLISH = 21950.0
EMA20_BEARISH = 22050.0
# Morning window default is 09:31-11:00 IST -- comfortably inside it.
BASE = datetime(2026, 7, 24, 10, 0, tzinfo=IST)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="ema-test-account",
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
        started_at=datetime.now(IST),
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
        symbol="NIFTY26JUL22000CE-EMA",
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
        symbol="NIFTY26JUL22000PE-EMA",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name="ema-test",
        strategy_type="ema_micro_pullback",
    )
    db.add(config)
    db.flush()
    return config


@pytest.fixture
def strategy_run(
    db: Session, strategy_config: StrategyConfig, trading_session, user: User
) -> StrategyRun:
    return _make_strategy_run(db, strategy_config, trading_session, user)


def _make_strategy_run(db: Session, strategy_config, trading_session, user: User) -> StrategyRun:
    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(IST),
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
    now = datetime.now(IST)
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
    db: Session, instrument: Instrument, bucket_start: datetime, *,
    open: float, high: float, low: float, close: float,  # noqa: A002
) -> PriceBar:
    bar = PriceBar(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        timeframe=BAR_TIMEFRAME,
        bucket_start=bucket_start,
        open=open, high=high, low=low, close=close,
        volume=1000,
    )
    db.add(bar)
    db.flush()
    return bar


def _seed_expansion(
    db: Session, instrument: Instrument, *, ema20: float, spreads: list[float], start: datetime,
) -> None:
    """`len(spreads)` EMA9/EMA20 pairs, oldest first, one minute apart --
    ema20 held constant, ema9 = ema20 + spread. `spreads` supplied
    oldest-to-newest, matching get_recent_indicator_values' own ordering.
    """
    for i, spread in enumerate(spreads):
        ts = start + timedelta(minutes=i)
        db.add(IndicatorSnapshot(
            id=uuid.uuid4(), instrument_id=instrument.id, indicator_name="EMA9",
            timeframe=BAR_TIMEFRAME, value=ema20 + spread, ts=ts,
        ))
        db.add(IndicatorSnapshot(
            id=uuid.uuid4(), instrument_id=instrument.id, indicator_name="EMA20",
            timeframe=BAR_TIMEFRAME, value=ema20, ts=ts,
        ))
    db.flush()


def _seed_filler_bars(
    db: Session, instrument: Instrument, *, count: int, start: datetime,
    open_: float, close_: float,
) -> None:
    """`count` bars with a large, consistent body/range ratio (0.8) so the
    body-ratio filter passes regardless of the setup/entry bars' own
    (typically smaller) bodies.
    """
    high_ = max(open_, close_) + 1
    low_ = min(open_, close_) - 1
    for i in range(count):
        _seed_bar(
            db, instrument, start + timedelta(minutes=i),
            open=open_, high=high_, low=low_, close=close_,
        )


def _seed_bullish_baseline(db: Session, instrument: Instrument) -> tuple[PriceBar, PriceBar]:
    """A fully valid bullish setup: 3 expanding EMA spreads (20 -> 35 -> 50,
    all positive), 8 filler bars with healthy bodies, then a setup bar whose
    low lands in the EMA9/EMA20 bone zone and closes above EMA20, followed
    by an entry bar closing above the setup bar's high."""
    _seed_expansion(
        db, instrument, ema20=EMA20_BULLISH, spreads=[20.0, 35.0, 50.0],
        start=BASE - timedelta(minutes=10),
    )
    _seed_filler_bars(
        db, instrument, count=BODY_RATIO_LOOKBACK_BARS - 2,
        start=BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS), open_=21900, close_=21908,
    )
    setup_bar = _seed_bar(
        db, instrument, BASE - timedelta(minutes=1),
        open=21980, high=21995, low=21960, close=21985,
    )
    entry_bar = _seed_bar(
        db, instrument, BASE,
        open=21990, high=22015, low=21988, close=22010,
    )
    return setup_bar, entry_bar


def _seed_bearish_baseline(db: Session, instrument: Instrument) -> tuple[PriceBar, PriceBar]:
    _seed_expansion(
        db, instrument, ema20=EMA20_BEARISH, spreads=[-20.0, -35.0, -50.0],
        start=BASE - timedelta(minutes=10),
    )
    _seed_filler_bars(
        db, instrument, count=BODY_RATIO_LOOKBACK_BARS - 2,
        start=BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS), open_=22100, close_=22092,
    )
    setup_bar = _seed_bar(
        db, instrument, BASE - timedelta(minutes=1),
        open=22020, high=22035, low=22005, close=22015,
    )
    entry_bar = _seed_bar(
        db, instrument, BASE,
        open=22010, high=22012, low=21985, close=21990,
    )
    return setup_bar, entry_bar


class TestEMAMicroPullbackStrategy:
    def test_bullish_setup_fires_buy_ce(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        setup_bar, entry_bar = _seed_bullish_baseline(db, instrument)

        strategy = EMAMicroPullbackStrategy(instrument.id, EXPIRY)
        proposal = strategy.check_setup(db, strategy_run, entry_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.structure_level == pytest.approx(float(setup_bar.low))
        assert strategy.trades_fired_count == 1

    def test_bearish_setup_fires_buy_pe(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        setup_bar, entry_bar = _seed_bearish_baseline(db, instrument)

        strategy = EMAMicroPullbackStrategy(instrument.id, EXPIRY)
        proposal = strategy.check_setup(db, strategy_run, entry_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_pe.id
        assert proposal.structure_level == pytest.approx(float(setup_bar.high))
        assert strategy.trades_fired_count == 1

    def test_no_signal_without_ema_warmup(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Only 2 of the default ema_expansion_lookback=3 EMA9/EMA20 pairs
        exist -- must wait for the third, same "insufficient history"
        philosophy as every other strategy's warmup guard."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_expansion(
            db, instrument, ema20=EMA20_BULLISH, spreads=[20.0, 35.0],
            start=BASE - timedelta(minutes=10),
        )
        _seed_filler_bars(
            db, instrument, count=BODY_RATIO_LOOKBACK_BARS - 2,
            start=BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS), open_=21900, close_=21908,
        )
        _seed_bar(db, instrument, BASE - timedelta(minutes=1),
                  open=21980, high=21995, low=21960, close=21985)
        entry_bar = _seed_bar(db, instrument, BASE,
                               open=21990, high=22015, low=21988, close=22010)

        strategy = EMAMicroPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, entry_bar) is None

    def test_no_signal_when_bullish_expansion_is_flat(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Same positive spread three times in a row (20, 20, 20) -- a
        stable, non-accelerating gap, not a genuine expansion."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_expansion(
            db, instrument, ema20=EMA20_BULLISH, spreads=[20.0, 20.0, 20.0],
            start=BASE - timedelta(minutes=10),
        )
        _seed_filler_bars(
            db, instrument, count=BODY_RATIO_LOOKBACK_BARS - 2,
            start=BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS), open_=21900, close_=21908,
        )
        _seed_bar(db, instrument, BASE - timedelta(minutes=1),
                  open=21980, high=21995, low=21960, close=21985)
        entry_bar = _seed_bar(db, instrument, BASE,
                               open=21990, high=22015, low=21988, close=22010)

        strategy = EMAMicroPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, entry_bar) is None

    def test_no_signal_when_bearish_expansion_is_narrowing(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Spread magnitude shrinking (-50, -35, -20) -- the trend is
        fading, not accelerating, even though every value is negative."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_expansion(
            db, instrument, ema20=EMA20_BEARISH, spreads=[-50.0, -35.0, -20.0],
            start=BASE - timedelta(minutes=10),
        )
        _seed_filler_bars(
            db, instrument, count=BODY_RATIO_LOOKBACK_BARS - 2,
            start=BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS), open_=22100, close_=22092,
        )
        _seed_bar(db, instrument, BASE - timedelta(minutes=1),
                  open=22020, high=22035, low=22005, close=22015)
        entry_bar = _seed_bar(db, instrument, BASE,
                               open=22010, high=22012, low=21985, close=21990)

        strategy = EMAMicroPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, entry_bar) is None

    def test_no_signal_when_setup_bar_low_outside_bone_zone(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Expansion is genuinely bullish, but the setup bar's low never
        dips into [ema20, ema9] -- no real pullback happened, so Bone Zone
        must block even with a clean confirmation bar after it. Exercises
        the setup_bar=bars[-2]/entry_bar=bars[-1] indexing explicitly."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_expansion(
            db, instrument, ema20=EMA20_BULLISH, spreads=[20.0, 35.0, 50.0],
            start=BASE - timedelta(minutes=10),
        )
        _seed_filler_bars(
            db, instrument, count=BODY_RATIO_LOOKBACK_BARS - 2,
            start=BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS), open_=21900, close_=21908,
        )
        # Setup bar's low (22010) stays well above the zone [21950, 22000].
        _seed_bar(db, instrument, BASE - timedelta(minutes=1),
                  open=22015, high=22025, low=22010, close=22020)
        entry_bar = _seed_bar(db, instrument, BASE,
                               open=22020, high=22040, low=22018, close=22035)

        strategy = EMAMicroPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, entry_bar) is None

    def test_no_signal_when_body_ratio_too_low(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """A clean bullish expansion + bone zone pullback, but the other 8
        bars are all small-bodied/wicky -- the chop filter must still
        block it."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_expansion(
            db, instrument, ema20=EMA20_BULLISH, spreads=[20.0, 35.0, 50.0],
            start=BASE - timedelta(minutes=10),
        )
        # body=1, range=20 -> ratio 0.05, well under the 0.40 default.
        start = BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS)
        for i in range(BODY_RATIO_LOOKBACK_BARS - 2):
            _seed_bar(
                db, instrument, start + timedelta(minutes=i),
                open=21900, high=21910, low=21890, close=21901,
            )
        _seed_bar(db, instrument, BASE - timedelta(minutes=1),
                  open=21980, high=21995, low=21960, close=21985)
        entry_bar = _seed_bar(db, instrument, BASE,
                               open=21990, high=22015, low=21988, close=22010)

        strategy = EMAMicroPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, entry_bar) is None

    def test_zero_average_range_does_not_raise_and_blocks_entry(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """All 10 bars perfectly flat (high == low == open == close) --
        avg_range == 0.0 must map to body_ratio = 0.0, not a
        ZeroDivisionError, and must still correctly fail the filter."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_expansion(
            db, instrument, ema20=EMA20_BULLISH, spreads=[20.0, 35.0, 50.0],
            start=BASE - timedelta(minutes=10),
        )
        start = BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS)
        for i in range(BODY_RATIO_LOOKBACK_BARS):
            _seed_bar(
                db, instrument, start + timedelta(minutes=i),
                open=21985, high=21985, low=21985, close=21985,
            )
        entry_bar = _seed_bar(db, instrument, BASE, open=21985, high=21985, low=21985, close=21985)

        strategy = EMAMicroPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, entry_bar) is None

    def test_no_signal_outside_trade_windows(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """A fully valid bullish setup, but the entry bar's IST time
        (12:00) falls in the deliberate midday gap between the default
        09:31-11:00 morning window and 13:00-15:00 afternoon window."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        midday = datetime(2026, 7, 24, 12, 0, tzinfo=IST)
        _seed_expansion(
            db, instrument, ema20=EMA20_BULLISH, spreads=[20.0, 35.0, 50.0],
            start=midday - timedelta(minutes=10),
        )
        _seed_filler_bars(
            db, instrument, count=BODY_RATIO_LOOKBACK_BARS - 2,
            start=midday - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS), open_=21900, close_=21908,
        )
        _seed_bar(db, instrument, midday - timedelta(minutes=1),
                  open=21980, high=21995, low=21960, close=21985)
        entry_bar = _seed_bar(db, instrument, midday,
                               open=21990, high=22015, low=21988, close=22010)

        strategy = EMAMicroPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, entry_bar) is None

    def test_no_signal_once_max_trades_per_session_reached(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _, entry_bar = _seed_bullish_baseline(db, instrument)

        strategy = EMAMicroPullbackStrategy(instrument.id, EXPIRY, ema_max_trades_per_session=1)
        first = strategy.check_setup(db, strategy_run, entry_bar)
        assert first is not None
        assert strategy.trades_fired_count == 1

        second = strategy.check_setup(db, strategy_run, entry_bar)
        assert second is None

    def test_trades_fired_count_resets_on_new_strategy_run(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_config,
        trading_session, user,
    ):
        """Defensive coverage for the spec's own reset-on-run-change
        requirement -- not reachable via the real start_strategy path
        today (a fresh Strategy instance is always constructed per
        StrategyRun), but proven correct here in case that ever changes.
        """
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _, entry_bar = _seed_bullish_baseline(db, instrument)

        strategy = EMAMicroPullbackStrategy(instrument.id, EXPIRY, ema_max_trades_per_session=1)
        run_a = _make_strategy_run(db, strategy_config, trading_session, user)
        proposal_a = strategy.check_setup(db, run_a, entry_bar)
        assert proposal_a is not None
        assert strategy.trades_fired_count == 1

        # Same strategy instance, a different StrategyRun -- the cap must
        # not carry over.
        run_b = _make_strategy_run(db, strategy_config, trading_session, user)
        proposal_b = strategy.check_setup(db, run_b, entry_bar)
        assert proposal_b is not None
        assert strategy.trades_fired_count == 1
