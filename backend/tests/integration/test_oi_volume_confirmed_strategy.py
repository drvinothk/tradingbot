"""OIVolumeConfirmedStrategy.check_setup — pure entry-logic tests against
constructed PriceBar/OptionChainSnapshot fixtures. Same "test the setup logic
in isolation" split test_orb_strategy.py uses.
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
from app.modules.strategy_engine.strategies.oi_volume_confirmed import (
    BODY_RATIO_LOOKBACK_BARS,
    OIVolumeConfirmedStrategy,
)

EXPIRY = date(2026, 7, 30)
LOOKBACK_BARS = 5
# Default NIFTY range filter is [15, 60] -- comfortably inside it.
WINDOW_HIGH = 22030.0
WINDOW_LOW = 21990.0
# Morning window default is 09:31-11:00 IST -- comfortably inside it.
BASE = datetime(2026, 7, 24, 10, 0, tzinfo=IST)
FILLER_BARS = BODY_RATIO_LOOKBACK_BARS - LOOKBACK_BARS - 1  # 4, padding up to 10 total


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
    oi: int = 20000,
    volume: int = 5000,
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


def _seed_window_and_filler(
    db: Session, instrument: Instrument, start: datetime, *,
    window_high: float = WINDOW_HIGH, window_low: float = WINDOW_LOW,
) -> None:
    """`FILLER_BARS` bars with healthy bodies, then `LOOKBACK_BARS` window
    bars (extremes touching `window_high`/`window_low` exactly once each,
    bodies large enough that the aggregate 10-bar body ratio clears the
    default 0.40 filter alongside the bar-under-test)."""
    for i in range(FILLER_BARS):
        _seed_bar(
            db, instrument, start + timedelta(minutes=i),
            open=21900, high=21909, low=21899, close=21908,
        )
    mid = (window_high + window_low) / 2
    q = (window_high - window_low) / 4
    window_specs = [
        (mid, window_high, mid - q, mid + q),  # touches window_high
        (mid, mid + q, window_low, mid - q),  # touches window_low
        (mid - q, mid + q, mid - q, mid + q),
        (mid + q, mid + q, mid - q, mid - q),
        (mid - q, mid + q, mid - q, mid + q),
    ]
    for i, (o, h, low, c) in enumerate(window_specs[:LOOKBACK_BARS]):
        _seed_bar(
            db, instrument, start + timedelta(minutes=FILLER_BARS + i),
            open=o, high=h, low=low, close=c,
        )


def _seed_atr(db: Session, instrument: Instrument, value: float) -> None:
    db.add(IndicatorSnapshot(
        id=uuid.uuid4(), instrument_id=instrument.id, indicator_name="ATR14",
        timeframe=BAR_TIMEFRAME, value=value, ts=datetime.now(IST),
    ))
    db.flush()


class TestOIVolumeConfirmedStrategy:
    def test_no_signal_with_insufficient_bars(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        bar = _seed_bar(db, instrument, BASE, open=22000, high=22010, low=21990, close=22005)

        assert strategy.check_setup(db, strategy_run, bar) is None

    def test_no_breakout_when_price_stays_within_window(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(db, instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS))

        inside_bar = _seed_bar(
            db, instrument, BASE, open=22005, high=22020, low=21995, close=22010,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, inside_bar) is None

    def test_bullish_breakout_fires_buy_ce(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(db, instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS))

        breakout_bar = _seed_bar(
            db, instrument, BASE, open=22030, high=22055, low=22028, close=22050,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.structure_level == pytest.approx(WINDOW_LOW)
        assert proposal.stop_price < proposal.entry_price < proposal.target_price
        assert proposal.structure_break_buffer == pytest.approx(0.0)

    def test_structure_break_buffer_is_atr_scaled_and_persistence_is_configurable(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(db, instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS))
        _seed_atr(db, instrument, value=10.0)

        breakout_bar = _seed_bar(
            db, instrument, BASE, open=22030, high=22055, low=22028, close=22050,
        )

        strategy = OIVolumeConfirmedStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
            structure_break_atr_multiplier=0.2, structure_break_persistence_seconds=2.0,
        )
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.structure_break_buffer == pytest.approx(2.0)  # 0.2 * 10.0
        assert proposal.structure_break_persistence_seconds == pytest.approx(2.0)
        assert strategy.trades_fired_count == 1

    def test_bearish_breakout_fires_buy_pe(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(db, instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS))

        breakout_bar = _seed_bar(
            db, instrument, BASE, open=21990, high=21995, low=21960, close=21965,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_pe.id
        assert proposal.structure_level == pytest.approx(WINDOW_HIGH)

    def test_breakout_direction_only_fires_once(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(db, instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS))

        breakout_bar = _seed_bar(
            db, instrument, BASE, open=22030, high=22055, low=22028, close=22050,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        first = strategy.check_setup(db, strategy_run, breakout_bar)
        second = strategy.check_setup(db, strategy_run, breakout_bar)

        assert first is not None
        assert second is None

    def test_breakout_blocked_when_participation_below_floor(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """The chain-participation-weighted ranking config's min_oi/min_volume
        floor (5000/500) excludes the top candidate outright, even though the
        price action itself would otherwise fire — proving the "confirmed"
        half of OI/Volume Confirmed actually gates the trade, not just scores
        it lower.
        """
        _seed_chain(
            db, instrument, option_contract_ce, option_contract_pe, oi=1000, volume=100
        )
        _seed_window_and_filler(db, instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS))

        breakout_bar = _seed_bar(
            db, instrument, BASE, open=22030, high=22055, low=22028, close=22050,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

    def test_pending_breakout_tracked_when_blocked_by_time_window(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """A raw breakout candidate outside the trade windows is blocked
        from firing, but must still be recorded as pending -- the whole
        point of the pre-emptive false-breakout gate."""
        midday = datetime(2026, 7, 24, 12, 0, tzinfo=IST)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(
            db, instrument, midday - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS),
        )
        breakout_bar = _seed_bar(
            db, instrument, midday, open=22030, high=22055, low=22028, close=22050,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None
        assert OptionType.CE in strategy._pending_breakout  # noqa: SLF001

    def test_reentry_within_grace_period_blocks_direction_permanently(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        midday = datetime(2026, 7, 24, 12, 0, tzinfo=IST)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(
            db, instrument, midday - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS),
        )
        breakout_bar = _seed_bar(
            db, instrument, midday, open=22030, high=22055, low=22028, close=22050,
        )
        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

        # One bar later, price falls back inside the *frozen* [21990, 22030]
        # snapshot -- confirms the breakout was false.
        reentry_bar = _seed_bar(
            db, instrument, midday + timedelta(minutes=1),
            open=22010, high=22015, low=22005, close=22010,
        )
        assert strategy.check_setup(db, strategy_run, reentry_bar) is None
        assert OptionType.CE in strategy._false_breakout_blocked  # noqa: SLF001
        assert OptionType.CE not in strategy._pending_breakout  # noqa: SLF001

    def test_false_breakout_blocked_direction_never_fires(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """A direction already confirmed false must not fire even against
        an otherwise fully valid setup."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(db, instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS))
        breakout_bar = _seed_bar(
            db, instrument, BASE, open=22030, high=22055, low=22028, close=22050,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        # Pre-seed the run-change guard too, or check_setup's own reset
        # (this being the first call with this strategy_run.id) would wipe
        # the manually-set block before it has a chance to matter.
        strategy._current_run_id = strategy_run.id  # noqa: SLF001
        strategy._false_breakout_blocked.add(OptionType.CE)  # noqa: SLF001

        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

    def test_pending_breakout_expires_without_penalty_after_grace_period(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Price never re-enters the frozen range within 3 bars -- the
        pending entry just expires, no block."""
        midday = datetime(2026, 7, 24, 12, 0, tzinfo=IST)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(
            db, instrument, midday - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS),
        )
        breakout_bar = _seed_bar(
            db, instrument, midday, open=22030, high=22055, low=22028, close=22050,
        )
        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None
        assert OptionType.CE in strategy._pending_breakout  # noqa: SLF001

        for i in range(1, 5):
            bar = _seed_bar(
                db, instrument, midday + timedelta(minutes=i),
                open=22050, high=22060, low=22045, close=22055,
            )
            strategy.check_setup(db, strategy_run, bar)

        assert OptionType.CE not in strategy._pending_breakout  # noqa: SLF001
        assert OptionType.CE not in strategy._false_breakout_blocked  # noqa: SLF001

    def test_no_signal_when_range_too_narrow(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Width 10 is below the NIFTY default min (15 points)."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(
            db, instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS),
            window_high=22005.0, window_low=21995.0,
        )
        breakout_bar = _seed_bar(
            db, instrument, BASE, open=22005, high=22020, low=22000, close=22015,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

    def test_no_signal_when_range_too_wide(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Width 200 is above the NIFTY default max (60 points)."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(
            db, instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS),
            window_high=22100.0, window_low=21900.0,
        )
        breakout_bar = _seed_bar(
            db, instrument, BASE, open=22100, high=22160, low=22095, close=22150,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

    def test_range_filter_uses_banknifty_thresholds_for_banknifty_instrument(
        self, db: Session, trading_session, strategy_run,
    ):
        """A 100-point range fails NIFTY's default max (60) but is well
        within BANKNIFTY's default band (50-180)."""
        bn_instrument = Instrument(
            id=uuid.uuid4(), symbol="BANKNIFTY", exchange="NFO", lot_size=15, tick_size=0.05,
        )
        db.add(bn_instrument)
        db.flush()
        ce = OptionContract(
            id=uuid.uuid4(), instrument_id=bn_instrument.id, expiry_date=EXPIRY,
            strike=48000, option_type=OptionType.CE, symbol="BANKNIFTY26JUL48000CE-OIVOL",
        )
        pe = OptionContract(
            id=uuid.uuid4(), instrument_id=bn_instrument.id, expiry_date=EXPIRY,
            strike=48000, option_type=OptionType.PE, symbol="BANKNIFTY26JUL48000PE-OIVOL",
        )
        db.add_all([ce, pe])
        db.flush()
        _seed_chain(db, bn_instrument, ce, pe, spot=48000.0)
        _seed_window_and_filler(
            db, bn_instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS),
            window_high=48100.0, window_low=48000.0,
        )
        breakout_bar = _seed_bar(
            db, bn_instrument, BASE, open=48100, high=48160, low=48095, close=48150,
        )

        strategy = OIVolumeConfirmedStrategy(bn_instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == ce.id

    def test_no_signal_when_body_ratio_too_low(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        start = BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS)
        # All 9 bars small-bodied/wicky: body=1, range=20 -> ratio 0.05.
        for i in range(BODY_RATIO_LOOKBACK_BARS - 1):
            _seed_bar(
                db, instrument, start + timedelta(minutes=i),
                open=22000 if i != FILLER_BARS + 1 else 22020,
                high=22010, low=21990, close=22001,
            )
        breakout_bar = _seed_bar(
            db, instrument, BASE, open=22030, high=22055, low=22028, close=22050,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

    def test_zero_average_range_does_not_raise_and_blocks_entry(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        start = BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS)
        for i in range(BODY_RATIO_LOOKBACK_BARS):
            _seed_bar(
                db, instrument, start + timedelta(minutes=i),
                open=22000, high=22000, low=22000, close=22000,
            )
        entry_bar = _seed_bar(db, instrument, BASE, open=22000, high=22000, low=22000, close=22000)

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, entry_bar) is None

    def test_no_signal_outside_trade_windows(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        midday = datetime(2026, 7, 24, 12, 0, tzinfo=IST)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(
            db, instrument, midday - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS),
        )
        breakout_bar = _seed_bar(
            db, instrument, midday, open=22030, high=22055, low=22028, close=22050,
        )

        strategy = OIVolumeConfirmedStrategy(instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS)
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

    def test_no_signal_once_max_trades_per_session_reached(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(db, instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS))
        breakout_bar = _seed_bar(
            db, instrument, BASE, open=22030, high=22055, low=22028, close=22050,
        )

        strategy = OIVolumeConfirmedStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS, oi_max_trades_per_session=1,
        )
        first = strategy.check_setup(db, strategy_run, breakout_bar)
        assert first is not None
        assert strategy.trades_fired_count == 1

        # Same direction is already in _fired_directions regardless, but
        # confirms the cap independently would also have blocked it.
        strategy._fired_directions.clear()  # noqa: SLF001
        second = strategy.check_setup(db, strategy_run, breakout_bar)
        assert second is None

    def test_trades_fired_count_resets_on_new_strategy_run(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_config,
        trading_session, user,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_window_and_filler(db, instrument, BASE - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS))
        breakout_bar = _seed_bar(
            db, instrument, BASE, open=22030, high=22055, low=22028, close=22050,
        )

        strategy = OIVolumeConfirmedStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS, oi_max_trades_per_session=1,
        )
        run_a = _make_strategy_run(db, strategy_config, trading_session, user)
        proposal_a = strategy.check_setup(db, run_a, breakout_bar)
        assert proposal_a is not None

        run_b = _make_strategy_run(db, strategy_config, trading_session, user)
        proposal_b = strategy.check_setup(db, run_b, breakout_bar)
        assert proposal_b is not None
        assert strategy.trades_fired_count == 1
