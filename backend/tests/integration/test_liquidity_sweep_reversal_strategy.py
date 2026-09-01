"""LiquiditySweepReversalStrategy.check_setup — pure entry-logic tests
against constructed PriceBar/OptionChainSnapshot fixtures. Same "test the
setup logic in isolation" split test_orb_strategy.py uses.
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
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import ExecutionMode, StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.strategy_engine.common_rules import BAR_TIMEFRAME
from app.modules.strategy_engine.strategies.liquidity_sweep_reversal import (
    LiquiditySweepReversalStrategy,
)

EXPIRY = date(2026, 7, 30)
LOOKBACK_BARS = 10
# Default NIFTY range filter is [30, 120] -- 100 is comfortably inside it.
WINDOW_HIGH = 22050.0
WINDOW_LOW = 21950.0
# Morning window default is 09:31-11:00 IST -- comfortably inside it. This
# is the *confirmation* bar's time; the sweep bar is one minute earlier.
BASE = datetime(2026, 7, 24, 10, 0, tzinfo=IST)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="sweep-test-account",
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
        symbol="NIFTY26JUL22000CE-SWEEP",
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
        symbol="NIFTY26JUL22000PE-SWEEP",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="sweep-test",
        strategy_type="liquidity_sweep_reversal",
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
    from app.domain.market.models import QuoteTick as QuoteTickRow

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
    db: Session, instrument: Instrument, start: datetime, *,
    window_high: float = WINDOW_HIGH, window_low: float = WINDOW_LOW,
) -> None:
    """`LOOKBACK_BARS` bars (extremes touching `window_high`/`window_low`
    at least once, bodies large enough that the aggregate 10-bar body ratio
    at confirmation time clears the default 0.40 filter)."""
    mid = (window_high + window_low) / 2
    q = (window_high - window_low) / 4
    specs = [
        (mid, window_high, mid - q, mid + q),  # touches window_high
        (mid, mid + q, window_low, mid - q),  # touches window_low
        (mid - q, mid + q, mid - q, mid + q),
        (mid + q, mid + q, mid - q, mid - q),
        (mid - q, mid + q, mid - q, mid + q),
    ]
    for i in range(LOOKBACK_BARS):
        o, h, low, c = specs[i % len(specs)]
        _seed_bar(db, instrument, start + timedelta(minutes=i), open=o, high=h, low=low, close=c)


def _seed_bullish_sweep(
    db: Session, instrument: Instrument, *,
    window_high: float = WINDOW_HIGH, window_low: float = WINDOW_LOW,
    sweep_low: float | None = None, base: datetime = BASE,
) -> PriceBar:
    """Window, then a sweep bar (wicks below window_low, closes back
    inside) -- deliberately does NOT also seed the confirmation bar: the
    sweep bar's own check_setup call must run first, while it's genuinely
    the latest persisted bar. Seeding the confirmation bar too early would
    make the sweep bar's own get_recent_completed_bars query see the
    confirmation bar as "latest" instead, corrupting the computed window.
    `base` shifts the whole sequence to a different point in time -- needed
    when seeding a second sweep+confirmation in the same test, since the
    default always anchors to the same fixed BASE timestamps.
    """
    start = base - timedelta(minutes=LOOKBACK_BARS + 2)
    _seed_window(db, instrument, start, window_high=window_high, window_low=window_low)
    low = sweep_low if sweep_low is not None else window_low - 15
    return _seed_bar(
        db, instrument, base - timedelta(minutes=1),
        open=window_low + 20, high=window_low + 30, low=low, close=window_low + 10,
    )


def _seed_atr(db: Session, instrument: Instrument, value: float) -> None:
    db.add(IndicatorSnapshot(
        id=uuid.uuid4(), instrument_id=instrument.id, indicator_name="ATR14",
        timeframe=BAR_TIMEFRAME, value=value, ts=datetime.now(IST),
    ))
    db.flush()


def _seed_bullish_confirmation(
    db: Session, instrument: Instrument, sweep_bar: PriceBar, *, base: datetime = BASE,
) -> PriceBar:
    """Closes above the sweep bar's own high -- call only after the sweep
    bar has already been through check_setup once."""
    return _seed_bar(
        db, instrument, base,
        open=float(sweep_bar.high) + 5, high=float(sweep_bar.high) + 30,
        low=float(sweep_bar.high), close=float(sweep_bar.high) + 25,
    )


def _seed_bearish_sweep(
    db: Session, instrument: Instrument, *,
    window_high: float = WINDOW_HIGH, window_low: float = WINDOW_LOW,
) -> PriceBar:
    """Window, then a sweep bar (wicks above window_high, closes back
    inside) -- see `_seed_bullish_sweep`'s own docstring for why the
    confirmation bar is deliberately not seeded here too."""
    start = BASE - timedelta(minutes=LOOKBACK_BARS + 2)
    _seed_window(db, instrument, start, window_high=window_high, window_low=window_low)
    return _seed_bar(
        db, instrument, BASE - timedelta(minutes=1),
        open=window_high - 20, high=window_high + 20, low=window_high - 25, close=window_high - 10,
    )


def _seed_bearish_confirmation(
    db: Session, instrument: Instrument, sweep_bar: PriceBar,
) -> PriceBar:
    """Closes below the sweep bar's own low -- call only after the sweep
    bar has already been through check_setup once."""
    return _seed_bar(
        db, instrument, BASE,
        open=float(sweep_bar.low) - 5, high=float(sweep_bar.low),
        low=float(sweep_bar.low) - 30, close=float(sweep_bar.low) - 25,
    )


class TestLiquiditySweepReversalStrategy:
    def test_no_signal_with_insufficient_bars(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        bar = _seed_bar(db, instrument, BASE, open=22000, high=22010, low=21990, close=22005)

        assert strategy.check_setup(db, strategy_run, bar) is None

    def test_no_signal_when_price_stays_within_window(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window(db, instrument, BASE - timedelta(minutes=LOOKBACK_BARS))
        inside_bar = _seed_bar(
            db, instrument, BASE, open=22000, high=22020, low=21980, close=22010,
        )

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, inside_bar) is None

    def test_genuine_breakout_that_closes_beyond_range_does_not_fire(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """A close beyond the window is a real breakout, not a sweep — this
        strategy's territory is a false breakout that reverses."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window(db, instrument, BASE - timedelta(minutes=LOOKBACK_BARS))
        breakout_bar = _seed_bar(
            db, instrument, BASE, open=22050, high=22080, low=22045, close=22070,
        )

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

    def test_sweep_alone_does_not_fire_without_confirmation(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        start = BASE - timedelta(minutes=LOOKBACK_BARS)
        _seed_window(db, instrument, start)
        sweep_bar = _seed_bar(
            db, instrument, start + timedelta(minutes=LOOKBACK_BARS),
            open=21970, high=21980, low=21930, close=21960,
        )

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None

    def test_swept_low_confirmed_next_bar_fires_buy_ce(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        sweep_bar = _seed_bullish_sweep(db, instrument)

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        confirmation_bar = _seed_bullish_confirmation(db, instrument, sweep_bar)
        proposal = strategy.check_setup(db, strategy_run, confirmation_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.structure_level == pytest.approx(WINDOW_LOW)
        assert proposal.stop_price < proposal.entry_price < proposal.target_price
        assert proposal.structure_break_buffer == pytest.approx(0.0)

    def test_structure_break_buffer_is_atr_scaled_and_persistence_is_configurable(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        sweep_bar = _seed_bullish_sweep(db, instrument)
        _seed_atr(db, instrument, value=10.0)

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
            structure_break_atr_multiplier=0.2, structure_break_persistence_seconds=8.0,
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        confirmation_bar = _seed_bullish_confirmation(db, instrument, sweep_bar)
        proposal = strategy.check_setup(db, strategy_run, confirmation_bar)

        assert proposal is not None
        assert proposal.structure_break_buffer == pytest.approx(2.0)  # 0.2 * 10.0
        assert proposal.structure_break_persistence_seconds == pytest.approx(8.0)

    def test_swept_high_confirmed_next_bar_fires_buy_pe(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        sweep_bar = _seed_bearish_sweep(db, instrument)

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        confirmation_bar = _seed_bearish_confirmation(db, instrument, sweep_bar)
        proposal = strategy.check_setup(db, strategy_run, confirmation_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_pe.id
        assert proposal.structure_level == pytest.approx(WINDOW_HIGH)

    def test_confirmation_that_fails_to_close_past_sweep_extreme_does_not_fire(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """A bullish sweep, but the next bar closes below (not above) the
        sweep candle's own high -- the corrected Option A rule (not the
        original, inverted wording) must reject this."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        sweep_bar = _seed_bullish_sweep(db, instrument)

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        weak_confirmation = _seed_bar(
            db, instrument, BASE,
            open=21965, high=float(sweep_bar.high) - 5, low=21955, close=21970,
        )
        assert strategy.check_setup(db, strategy_run, weak_confirmation) is None

    def test_can_fire_again_after_a_prior_confirmed_sweep_this_run(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Unlike ORB/OI-Volume-Confirmed, no per-direction fired-once guard
        — a second genuine sweep-and-reversal setup later in the same run
        must still fire."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        sweep_bar = _seed_bullish_sweep(db, instrument)

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        confirmation_bar = _seed_bullish_confirmation(db, instrument, sweep_bar)
        first = strategy.check_setup(db, strategy_run, confirmation_bar)
        assert first is not None

        second_base = BASE + timedelta(minutes=20)
        second_sweep = _seed_bullish_sweep(db, instrument, base=second_base)
        assert strategy.check_setup(db, strategy_run, second_sweep) is None
        second_confirmation = _seed_bullish_confirmation(
            db, instrument, second_sweep, base=second_base
        )
        second = strategy.check_setup(db, strategy_run, second_confirmation)
        assert second is not None

    def test_no_signal_when_sweep_distance_too_small(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Wick pokes only 2 points beyond window_low -- below the NIFTY
        default min_sweep_distance of 5."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        sweep_bar = _seed_bullish_sweep(db, instrument, sweep_low=WINDOW_LOW - 2)

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        assert strategy._pending_sweep is None  # noqa: SLF001

    def test_no_signal_when_range_too_narrow(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Width 20 is below the NIFTY default min (30 points)."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        sweep_bar = _seed_bullish_sweep(
            db, instrument, window_high=21990.0, window_low=21970.0, sweep_low=21955.0,
        )

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        assert strategy._pending_sweep is None  # noqa: SLF001

    def test_no_signal_when_range_too_wide(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Width 200 is above the NIFTY default max (120 points)."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        sweep_bar = _seed_bullish_sweep(
            db, instrument, window_high=22100.0, window_low=21900.0, sweep_low=21880.0,
        )

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        assert strategy._pending_sweep is None  # noqa: SLF001

    def test_range_filter_uses_banknifty_thresholds_for_banknifty_instrument(
        self, db: Session, trading_session, strategy_run,
    ):
        """A 200-point range fails NIFTY's default max (120) but is well
        within BANKNIFTY's default band (100-360)."""
        bn_instrument = Instrument(
            id=uuid.uuid4(), symbol="BANKNIFTY", exchange="NFO", lot_size=15, tick_size=0.05,
        )
        db.add(bn_instrument)
        db.flush()
        ce = OptionContract(
            id=uuid.uuid4(), instrument_id=bn_instrument.id, expiry_date=EXPIRY,
            strike=48000, option_type=OptionType.CE, symbol="BANKNIFTY26JUL48000CE-SWEEP",
        )
        pe = OptionContract(
            id=uuid.uuid4(), instrument_id=bn_instrument.id, expiry_date=EXPIRY,
            strike=48000, option_type=OptionType.PE, symbol="BANKNIFTY26JUL48000PE-SWEEP",
        )
        db.add_all([ce, pe])
        db.flush()
        _seed_chain(db, bn_instrument, ce, pe, spot=48000.0)
        sweep_bar = _seed_bullish_sweep(
            db, bn_instrument, window_high=48100.0, window_low=47900.0, sweep_low=47870.0,
        )

        strategy = LiquiditySweepReversalStrategy(
            bn_instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        confirmation_bar = _seed_bullish_confirmation(db, bn_instrument, sweep_bar)
        proposal = strategy.check_setup(db, strategy_run, confirmation_bar)

        assert proposal is not None
        assert proposal.option_contract_id == ce.id

    def test_no_signal_when_body_ratio_too_low(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        start = BASE - timedelta(minutes=LOOKBACK_BARS + 2)
        # All 10 window bars small-bodied/wicky: body=1, range=20.
        for i in range(LOOKBACK_BARS):
            high = WINDOW_HIGH if i == 0 else 22010.0
            low = WINDOW_LOW if i == 1 else 21990.0
            _seed_bar(
                db, instrument, start + timedelta(minutes=i),
                open=22000, high=high, low=low, close=22001,
            )
        sweep_bar = _seed_bar(
            db, instrument, BASE - timedelta(minutes=1),
            open=21970, high=21975, low=21935, close=21965,
        )

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        confirmation_bar = _seed_bar(
            db, instrument, BASE, open=21976, high=21978, low=21975, close=21977,
        )
        assert strategy.check_setup(db, strategy_run, confirmation_bar) is None

    def test_zero_average_range_does_not_raise(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        start = BASE - timedelta(minutes=LOOKBACK_BARS + 2)
        for i in range(LOOKBACK_BARS):
            _seed_bar(
                db, instrument, start + timedelta(minutes=i),
                open=22000, high=22000, low=22000, close=22000,
            )
        sweep_bar = _seed_bar(
            db, instrument, BASE - timedelta(minutes=1),
            open=22000, high=22000, low=21935, close=22000,
        )

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        # window_high == window_low == 22000 -> range width 0, below the
        # min filter, so this never even reaches the body-ratio computation
        # -- still proves nothing raises on a fully flat window/sweep.
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        confirmation_bar = _seed_bar(
            db, instrument, BASE, open=22000, high=22000, low=22000, close=22000,
        )
        assert strategy.check_setup(db, strategy_run, confirmation_bar) is None

    def test_no_signal_when_confirmation_bar_outside_trade_windows(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        midday = datetime(2026, 7, 24, 12, 0, tzinfo=IST)
        start = midday - timedelta(minutes=LOOKBACK_BARS + 2)
        _seed_window(db, instrument, start)
        sweep_bar = _seed_bar(
            db, instrument, midday - timedelta(minutes=1),
            open=21970, high=21980, low=21935, close=21960,
        )

        strategy = LiquiditySweepReversalStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS, sweep_morning_window_end="11:00",
        )
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None
        confirmation_bar = _seed_bar(
            db, instrument, midday,
            open=float(sweep_bar.high) + 5, high=float(sweep_bar.high) + 30,
            low=float(sweep_bar.high), close=float(sweep_bar.high) + 25,
        )
        assert strategy.check_setup(db, strategy_run, confirmation_bar) is None

