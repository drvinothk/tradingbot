"""VWAPPullbackStrategy.check_setup — pure entry-logic tests against
constructed PriceBar/IndicatorSnapshot/OptionChainSnapshot fixtures, same
split as test_orb_strategy.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

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
from app.modules.strategy_engine.strategies.vwap_pullback import VWAPPullbackStrategy

EXPIRY = date(2026, 7, 30)
VWAP = 22000.0


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="vwap-test-account",
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
        symbol="NIFTY26JUL22000CE-VWAP",
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
        symbol="NIFTY26JUL22000PE-VWAP",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="vwap-test", strategy_type="vwap_pullback"
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


def _seed_chain(
    db: Session,
    instrument: Instrument,
    option_contract_ce: OptionContract,
    option_contract_pe: OptionContract,
    *,
    spot: float = VWAP,
    ce_ltp: float = 80.0,
    pe_ltp: float = 75.0,
) -> None:
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


def _seed_vwap(db: Session, instrument: Instrument, value: float = VWAP) -> None:
    db.add(
        IndicatorSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            indicator_name="VWAP",
            timeframe=BAR_TIMEFRAME,
            value=value,
            ts=datetime.now(UTC),
        )
    )
    db.flush()


def _seed_atr(db: Session, instrument: Instrument, value: float) -> None:
    db.add(
        IndicatorSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            indicator_name="ATR14",
            timeframe=BAR_TIMEFRAME,
            value=value,
            ts=datetime.now(UTC),
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


def _seed_trending_history(
    db: Session,
    instrument: Instrument,
    base: datetime,
    side: str,
    *,
    count: int = 18,
) -> None:
    """`count` bars, all closing consistently on one side of VWAP (no
    crosses), ending right before `base` -- the trend/choppiness filter's
    default `trend_lookback_bars=20` needs this much history in addition to
    the pullback+confirmation bars a test seeds itself, or `_trend_direction`
    returns None (insufficient history) regardless of how clean the actual
    touch+confirm pattern is.
    """
    price = VWAP + 20.0 if side == "bullish" else VWAP - 20.0
    for i in range(count, 0, -1):
        db.add(
            PriceBar(
                id=uuid.uuid4(),
                instrument_id=instrument.id,
                timeframe=BAR_TIMEFRAME,
                bucket_start=base - timedelta(minutes=i),
                open=price, high=price + 5, low=price - 5, close=price,
                volume=1000,
            )
        )
    db.flush()


class TestVWAPPullbackStrategy:
    def test_bullish_pullback_confirmation_fires_buy_ce(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_vwap(db, instrument, VWAP)
        base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        _seed_trending_history(db, instrument, base, "bullish")
        # Pullback bar: dips to touch VWAP (low right at VWAP), closes above it.
        _seed_bar(db, instrument, base, open=22015, high=22020, low=VWAP, close=22010)
        # Confirmation bar: closes above the pullback bar's high, still above VWAP.
        confirmation = _seed_bar(
            db, instrument, base + timedelta(minutes=1),
            open=22015, high=22035, low=22012, close=22030,
        )

        strategy = VWAPPullbackStrategy(instrument.id, EXPIRY)
        proposal = strategy.check_setup(db, strategy_run, confirmation)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.structure_level == pytest.approx(VWAP)
        # No ATR14 seeded -- buffer degrades to 0.0, not an error (see
        # resolve_structure_break_buffer's own docstring).
        assert proposal.structure_break_buffer == pytest.approx(0.0)

    def test_structure_break_buffer_is_atr_scaled_and_persistence_is_configurable(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_vwap(db, instrument, VWAP)
        _seed_atr(db, instrument, value=10.0)
        base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        _seed_trending_history(db, instrument, base, "bullish")
        _seed_bar(db, instrument, base, open=22015, high=22020, low=VWAP, close=22010)
        confirmation = _seed_bar(
            db, instrument, base + timedelta(minutes=1),
            open=22015, high=22035, low=22012, close=22030,
        )

        strategy = VWAPPullbackStrategy(
            instrument.id, EXPIRY,
            structure_break_atr_multiplier=0.2,
            structure_break_persistence_seconds=9.0,
        )
        proposal = strategy.check_setup(db, strategy_run, confirmation)

        assert proposal is not None
        assert proposal.structure_break_buffer == pytest.approx(2.0)  # 0.2 * 10.0
        assert proposal.structure_break_persistence_seconds == pytest.approx(9.0)

    def test_bearish_pullback_confirmation_fires_buy_pe(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_vwap(db, instrument, VWAP)
        base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        _seed_trending_history(db, instrument, base, "bearish")
        # Pullback bar: rallies to touch VWAP (high right at VWAP), closes below it.
        _seed_bar(db, instrument, base, open=21985, high=VWAP, low=21980, close=21990)
        confirmation = _seed_bar(
            db, instrument, base + timedelta(minutes=1),
            open=21985, high=21988, low=21965, close=21970,
        )

        strategy = VWAPPullbackStrategy(instrument.id, EXPIRY)
        proposal = strategy.check_setup(db, strategy_run, confirmation)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_pe.id
        assert proposal.structure_level == pytest.approx(VWAP)

    def test_no_signal_without_vwap_touch(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_vwap(db, instrument, VWAP)
        base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        # Stays well clear of VWAP the whole time — no pullback ever occurs.
        _seed_bar(db, instrument, base, open=22100, high=22120, low=22090, close=22110)
        confirmation = _seed_bar(
            db, instrument, base + timedelta(minutes=1),
            open=22110, high=22140, low=22105, close=22130,
        )

        strategy = VWAPPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, confirmation) is None

    def test_no_signal_when_trend_history_is_insufficient(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """A valid touch+confirm pattern with no trend history before it
        (fewer than trend_lookback_bars=20 total) -- the same "wait for next
        cycle, never trade off data we can't vouch for" philosophy as the
        option-chain freshness gate.
        """
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_vwap(db, instrument, VWAP)
        base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        _seed_bar(db, instrument, base, open=22015, high=22020, low=VWAP, close=22010)
        confirmation = _seed_bar(
            db, instrument, base + timedelta(minutes=1),
            open=22015, high=22035, low=22012, close=22030,
        )

        strategy = VWAPPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, confirmation) is None

    def test_trend_history_from_a_previous_day_is_not_counted(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """18 bars of a clean bullish trend seeded on the PREVIOUS IST
        calendar day must not count toward today's trend_lookback_bars=20 --
        otherwise a fresh trading day with only a couple of today's own bars
        would borrow yesterday's trend to justify a signal. Regression test
        for the cross-day bar-contamination bug in `_trend_direction`.
        """
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_vwap(db, instrument, VWAP)
        yesterday_base = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
        _seed_trending_history(db, instrument, yesterday_base, "bullish")
        today_base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        _seed_bar(db, instrument, today_base, open=22015, high=22020, low=VWAP, close=22010)
        confirmation = _seed_bar(
            db, instrument, today_base + timedelta(minutes=1),
            open=22015, high=22035, low=22012, close=22030,
        )

        strategy = VWAPPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, confirmation) is None

    def test_no_signal_when_market_is_choppy(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """18 bars alternating sides of VWAP (well over max_vwap_crosses_in_
        lookback=3), then an otherwise-valid bullish touch+confirm -- the
        filter must block on chop even though the setup itself is clean.
        """
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_vwap(db, instrument, VWAP)
        base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        for i in range(18, 0, -1):
            price = VWAP + 10.0 if i % 2 == 0 else VWAP - 10.0
            _seed_bar(
                db, instrument, base - timedelta(minutes=i),
                open=price, high=price + 5, low=price - 5, close=price,
            )
        _seed_bar(db, instrument, base, open=22015, high=22020, low=VWAP, close=22010)
        confirmation = _seed_bar(
            db, instrument, base + timedelta(minutes=1),
            open=22015, high=22035, low=22012, close=22030,
        )

        strategy = VWAPPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, confirmation) is None

    def test_no_signal_when_trend_conflicts_with_setup_direction(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """A clean bearish trend (18 bars below VWAP) followed by a valid
        *bullish* touch+confirm pattern -- the setup direction must match
        the prevailing trend, not just be internally consistent on its own.
        """
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_vwap(db, instrument, VWAP)
        base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        _seed_trending_history(db, instrument, base, "bearish")
        _seed_bar(db, instrument, base, open=22015, high=22020, low=VWAP, close=22010)
        confirmation = _seed_bar(
            db, instrument, base + timedelta(minutes=1),
            open=22015, high=22035, low=22012, close=22030,
        )

        strategy = VWAPPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, confirmation) is None

    def test_no_signal_without_indicator_warmup(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        _seed_bar(db, instrument, base, open=22015, high=22020, low=VWAP, close=22010)
        confirmation = _seed_bar(
            db, instrument, base + timedelta(minutes=1),
            open=22015, high=22035, low=22012, close=22030,
        )

        strategy = VWAPPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, confirmation) is None

    def test_no_signal_when_vwap_is_stale(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """A valid bullish touch+confirm pattern, but the only persisted VWAP
        row is far older than the bar being evaluated -- the live
        volume-weighted VWAP feed has effectively stopped (real incident
        2026-08-27: an index underlying with no traded volume). Sit out
        rather than trade a frozen scalar.
        """
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        db.add(
            IndicatorSnapshot(
                id=uuid.uuid4(),
                instrument_id=instrument.id,
                indicator_name="VWAP",
                timeframe=BAR_TIMEFRAME,
                value=VWAP,
                ts=base - timedelta(hours=1),  # 3600s stale vs the confirmation bar
            )
        )
        db.flush()
        _seed_trending_history(db, instrument, base, "bullish")
        _seed_bar(db, instrument, base, open=22015, high=22020, low=VWAP, close=22010)
        confirmation = _seed_bar(
            db, instrument, base + timedelta(minutes=1),
            open=22015, high=22035, low=22012, close=22030,
        )

        strategy = VWAPPullbackStrategy(instrument.id, EXPIRY)
        assert strategy.check_setup(db, strategy_run, confirmation) is None

    def test_signal_fires_when_vwap_is_fresh_relative_to_the_bar(
        self, db: Session, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Regression guard for the staleness gate: a VWAP row timestamped at
        the bar itself is well within tolerance and must not be filtered.
        """
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        db.add(
            IndicatorSnapshot(
                id=uuid.uuid4(),
                instrument_id=instrument.id,
                indicator_name="VWAP",
                timeframe=BAR_TIMEFRAME,
                value=VWAP,
                ts=base + timedelta(minutes=1),
            )
        )
        db.flush()
        _seed_trending_history(db, instrument, base, "bullish")
        _seed_bar(db, instrument, base, open=22015, high=22020, low=VWAP, close=22010)
        confirmation = _seed_bar(
            db, instrument, base + timedelta(minutes=1),
            open=22015, high=22035, low=22012, close=22030,
        )

        strategy = VWAPPullbackStrategy(instrument.id, EXPIRY)
        proposal = strategy.check_setup(db, strategy_run, confirmation)
        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
