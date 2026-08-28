"""ATRBreakoutStrategy.check_setup — entry-logic tests against constructed
PriceBar / IndicatorSnapshot / OptionChainSnapshot fixtures, same
"exercise check_setup directly" split as test_orb_strategy.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

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
    QuoteTick,
)
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import ExecutionMode, StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.strategy_engine.common_rules import BAR_TIMEFRAME
from app.modules.strategy_engine.strategies.atr_breakout import ATRBreakoutStrategy

EXPIRY = date(2026, 7, 30)
LOOKBACK = 5
DAY = date(2026, 7, 24)
SESSION_OPEN = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
# First bar that is both inside the entry window (>= 09:30) and has enough
# history behind it.
ENTRY_TS = datetime(2026, 7, 24, 9, 40, tzinfo=IST)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(), workspace_id=workspace.id, broker_type=BrokerType.SHOONYA,
        label="atr-test", credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    return account


@pytest.fixture
def trading_session(db: Session, workspace, broker_account, user: User) -> TradingSession:
    ts = TradingSession(
        id=uuid.uuid4(), workspace_id=workspace.id, broker_account_id=broker_account.id,
        started_by_user_id=user.id, mode=SafeMode.PAPER_ONLY, started_at=datetime.now(UTC),
        budget_amount=1_000_000, daily_target_profit=1_000_000, daily_loss_cap=1_000_000,
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
    c = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY,
        strike=22000, option_type=OptionType.CE, symbol="NIFTY26JUL22000CE-ATR",
    )
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def option_contract_pe(db: Session, instrument: Instrument) -> OptionContract:
    c = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY,
        strike=22000, option_type=OptionType.PE, symbol="NIFTY26JUL22000PE-ATR",
    )
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="atr-test", strategy_type="atr_breakout",
    )
    db.add(config)
    db.flush()
    return config


def _run(db, strategy_config, trading_session, user) -> StrategyRun:
    r = StrategyRun(
        id=uuid.uuid4(), strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id, execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING, started_at=SESSION_OPEN, started_by_user_id=user.id,
    )
    db.add(r)
    db.flush()
    return r


def _seed_chain(db, instrument, ce, pe) -> None:
    now = datetime.now(UTC)
    db.add(QuoteTick(
        id=uuid.uuid4(), instrument_id=instrument.id, ltp=22000.0, bid=21999.0, ask=22001.0,
        volume=10000, oi=None, ts=now,
    ))
    db.add(OptionChainSnapshot(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY, ts=now,
        chain_data=[
            {"contract_symbol": ce.symbol, "strike": 22000.0, "option_type": "CE",
             "ltp": 80.0, "bid": 79.5, "ask": 80.5, "volume": 5000, "oi": 20000},
            {"contract_symbol": pe.symbol, "strike": 22000.0, "option_type": "PE",
             "ltp": 75.0, "bid": 74.5, "ask": 75.5, "volume": 5000, "oi": 20000},
        ],
    ))
    db.flush()


def _seed_bar(db, instrument, ts, *, o, h, low, c) -> PriceBar:
    bar = PriceBar(
        id=uuid.uuid4(), instrument_id=instrument.id, timeframe=BAR_TIMEFRAME,
        bucket_start=ts, open=o, high=h, low=low, close=c, volume=1000,
    )
    db.add(bar)
    db.flush()
    return bar


def _seed_flat_window(db, instrument, *, high: float, low: float, n: int = 24) -> None:
    """n one-minute bars from SESSION_OPEN, all inside [low, high]."""
    mid = (high + low) / 2
    for i in range(n):
        _seed_bar(
            db, instrument, SESSION_OPEN + timedelta(minutes=i), o=mid, h=high, low=low, c=mid
        )


def _seed_atr(db, instrument, values: list[float]) -> None:
    for i, v in enumerate(values):
        db.add(IndicatorSnapshot(
            id=uuid.uuid4(), instrument_id=instrument.id, indicator_name="ATR14",
            timeframe=BAR_TIMEFRAME, value=v, ts=SESSION_OPEN + timedelta(minutes=i),
        ))
    db.flush()


def _breakout_bar(db, instrument, ts=ENTRY_TS, *, up: bool = True) -> PriceBar:
    if up:
        return _seed_bar(db, instrument, ts, o=22030, h=22070, low=22028, c=22060)
    return _seed_bar(db, instrument, ts, o=21970, h=21972, low=21930, c=21940)


EXPANDING_ATR = [8, 8, 9, 9, 10, 20]
FLAT_ATR = [10.0] * 6


class TestATRBreakout:
    def test_fires_ce_on_upside_breakout_with_expanding_atr(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _run(db, strategy_config, trading_session, user)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_flat_window(db, instrument, high=22020, low=21980)
        _seed_atr(db, instrument, EXPANDING_ATR)

        s = ATRBreakoutStrategy(
            instrument.id, EXPIRY, breakout_lookback_bars=LOOKBACK, atr_expansion_lookback=LOOKBACK,
        )
        p = s.check_setup(db, run, _breakout_bar(db, instrument))

        assert p is not None
        assert p.option_contract_id == option_contract_ce.id
        assert p.structure_level == pytest.approx(21980.0)
        assert p.stop_price < p.entry_price < p.target_price
        assert p.payload["strategy"] == "atr_breakout"

    def test_fires_pe_on_downside_breakout(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _run(db, strategy_config, trading_session, user)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_flat_window(db, instrument, high=22020, low=21980)
        _seed_atr(db, instrument, EXPANDING_ATR)

        s = ATRBreakoutStrategy(
            instrument.id, EXPIRY, breakout_lookback_bars=LOOKBACK, atr_expansion_lookback=LOOKBACK,
        )
        p = s.check_setup(db, run, _breakout_bar(db, instrument, up=False))

        assert p is not None
        assert p.option_contract_id == option_contract_pe.id
        assert p.structure_level == pytest.approx(22020.0)

    def test_blocked_when_atr_flat(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _run(db, strategy_config, trading_session, user)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_flat_window(db, instrument, high=22020, low=21980)
        _seed_atr(db, instrument, FLAT_ATR)

        s = ATRBreakoutStrategy(
            instrument.id, EXPIRY, breakout_lookback_bars=LOOKBACK, atr_expansion_lookback=LOOKBACK,
        )
        assert s.check_setup(db, run, _breakout_bar(db, instrument)) is None

    def test_blocked_when_atr_not_warmed_up(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _run(db, strategy_config, trading_session, user)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_flat_window(db, instrument, high=22020, low=21980)

        s = ATRBreakoutStrategy(
            instrument.id, EXPIRY, breakout_lookback_bars=LOOKBACK, atr_expansion_lookback=LOOKBACK,
        )
        assert s.check_setup(db, run, _breakout_bar(db, instrument)) is None

    def test_no_signal_when_close_inside_window(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _run(db, strategy_config, trading_session, user)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_flat_window(db, instrument, high=22020, low=21980)
        _seed_atr(db, instrument, EXPANDING_ATR)

        inside = _seed_bar(db, instrument, ENTRY_TS, o=22000, h=22019, low=21990, c=22010)
        s = ATRBreakoutStrategy(
            instrument.id, EXPIRY, breakout_lookback_bars=LOOKBACK, atr_expansion_lookback=LOOKBACK,
        )
        assert s.check_setup(db, run, inside) is None

    def test_direction_only_fires_once(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _run(db, strategy_config, trading_session, user)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_flat_window(db, instrument, high=22020, low=21980)
        _seed_atr(db, instrument, EXPANDING_ATR)

        s = ATRBreakoutStrategy(
            instrument.id, EXPIRY, breakout_lookback_bars=LOOKBACK, atr_expansion_lookback=LOOKBACK,
        )
        first = s.check_setup(db, run, _breakout_bar(db, instrument, ENTRY_TS))
        second = s.check_setup(
            db, run, _breakout_bar(db, instrument, ENTRY_TS + timedelta(minutes=1))
        )
        assert first is not None
        assert second is None

    def test_max_trades_per_session_cap(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _run(db, strategy_config, trading_session, user)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_flat_window(db, instrument, high=22020, low=21980)
        _seed_atr(db, instrument, EXPANDING_ATR)

        s = ATRBreakoutStrategy(
            instrument.id, EXPIRY, breakout_lookback_bars=LOOKBACK, atr_expansion_lookback=LOOKBACK,
            max_trades_per_session=1,
        )
        up = s.check_setup(db, run, _breakout_bar(db, instrument, ENTRY_TS))
        down = s.check_setup(
            db, run, _breakout_bar(db, instrument, ENTRY_TS + timedelta(minutes=1), up=False)
        )
        assert up is not None
        assert down is None

    def test_blocked_before_entry_start_time(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _run(db, strategy_config, trading_session, user)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_flat_window(db, instrument, high=22020, low=21980, n=6)
        _seed_atr(db, instrument, EXPANDING_ATR)

        early = _seed_bar(
            db, instrument, datetime(2026, 7, 24, 9, 25, tzinfo=IST),
            o=22030, h=22070, low=22028, c=22060,
        )
        s = ATRBreakoutStrategy(
            instrument.id, EXPIRY, breakout_lookback_bars=LOOKBACK, atr_expansion_lookback=LOOKBACK,
        )
        assert s.check_setup(db, run, early) is None

    def test_blocked_after_entry_cutoff_time(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _run(db, strategy_config, trading_session, user)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_flat_window(db, instrument, high=22020, low=21980, n=24)
        _seed_atr(db, instrument, EXPANDING_ATR)

        late = _seed_bar(
            db, instrument, datetime(2026, 7, 24, 14, 30, tzinfo=IST),
            o=22030, h=22070, low=22028, c=22060,
        )
        s = ATRBreakoutStrategy(
            instrument.id, EXPIRY, breakout_lookback_bars=LOOKBACK, atr_expansion_lookback=LOOKBACK,
        )
        assert s.check_setup(db, run, late) is None

    def test_blocked_when_window_below_range_floor(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _run(db, strategy_config, trading_session, user)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        # Window width 4 — below the NIFTY default floor of 15.
        _seed_flat_window(db, instrument, high=22002, low=21998)
        _seed_atr(db, instrument, EXPANDING_ATR)

        bar = _seed_bar(db, instrument, ENTRY_TS, o=22002, h=22040, low=22001, c=22030)
        s = ATRBreakoutStrategy(
            instrument.id, EXPIRY, breakout_lookback_bars=LOOKBACK, atr_expansion_lookback=LOOKBACK,
        )
        assert s.check_setup(db, run, bar) is None

    def test_target_r_multiple_overrides_target(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _run(db, strategy_config, trading_session, user)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_flat_window(db, instrument, high=22020, low=21980)
        _seed_atr(db, instrument, EXPANDING_ATR)

        s = ATRBreakoutStrategy(
            instrument.id, EXPIRY, breakout_lookback_bars=LOOKBACK, atr_expansion_lookback=LOOKBACK,
            target_r_multiple=3.0,
        )
        p = s.check_setup(db, run, _breakout_bar(db, instrument))
        assert p is not None
        risk = p.entry_price - p.stop_price
        assert p.target_price == pytest.approx(p.entry_price + 3.0 * risk, abs=0.05)


class TestBuildStrategyDispatch:
    def test_build_strategy_maps_atr_breakout_type(self, db, workspace):
        from app.api.v1.strategies import _build_strategy

        config = StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="d", strategy_type="atr_breakout",
            params={"breakout_lookback_bars": 40, "atr_expansion_min_ratio": 1.25,
                    "target_r_multiple": 2.0},
        )
        s = _build_strategy(config, uuid.uuid4(), EXPIRY)
        assert isinstance(s, ATRBreakoutStrategy)
        assert s.breakout_lookback_bars == 40
        assert s.atr_expansion_min_ratio == 1.25
        assert s.target_r_multiple == 2.0
