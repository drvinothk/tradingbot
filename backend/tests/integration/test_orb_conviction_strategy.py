"""ORBConvictionStrategy.check_setup — the conviction gates layered on top
of ORBStrategy. Baseline behaviour (no gates enabled) is already covered by
test_orb_strategy.py; these tests only exercise what the subclass adds.
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
from app.modules.strategy_engine.strategies.orb_conviction import ORBConvictionStrategy

EXPIRY = date(2026, 7, 30)
OR_MINUTES = 15
VIX_SYMBOL = "INDIA VIX"


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="orbc-test-account",
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
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY,
        strike=22000, option_type=OptionType.CE, symbol="NIFTY26JUL22000CE-ORBC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def option_contract_pe(db: Session, instrument: Instrument) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY,
        strike=22000, option_type=OptionType.PE, symbol="NIFTY26JUL22000PE-ORBC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="orbc-test",
        strategy_type="orb_conviction",
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
    db: Session, instrument: Instrument,
    option_contract_ce: OptionContract, option_contract_pe: OptionContract,
) -> None:
    now = datetime.now(UTC)
    db.add(QuoteTick(
        id=uuid.uuid4(), instrument_id=instrument.id, ltp=22000.0,
        bid=21999.0, ask=22001.0, volume=10000, oi=None, ts=now,
    ))
    db.add(OptionChainSnapshot(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY, ts=now,
        chain_data=[
            {
                "contract_symbol": option_contract_ce.symbol, "strike": 22000.0,
                "option_type": "CE", "ltp": 80.0, "bid": 79.5, "ask": 80.5,
                "volume": 5000, "oi": 20000,
            },
            {
                "contract_symbol": option_contract_pe.symbol, "strike": 22000.0,
                "option_type": "PE", "ltp": 75.0, "bid": 74.5, "ask": 75.5,
                "volume": 5000, "oi": 20000,
            },
        ],
    ))
    db.flush()


def _seed_bar(
    db: Session, instrument: Instrument, bucket_start: datetime,
    *, open: float, high: float, low: float, close: float, volume: int = 1000,  # noqa: A002
) -> PriceBar:
    bar = PriceBar(
        id=uuid.uuid4(), instrument_id=instrument.id, timeframe=BAR_TIMEFRAME,
        bucket_start=bucket_start, open=open, high=high, low=low, close=close, volume=volume,
    )
    db.add(bar)
    db.flush()
    return bar


def _seed_opening_range(
    db: Session, instrument: Instrument, or_start: datetime, *, or_high: float, or_low: float
) -> None:
    mid = (or_high + or_low) / 2
    _seed_bar(db, instrument, or_start, open=mid, high=or_high, low=or_low, close=mid)
    for i in range(1, OR_MINUTES):
        _seed_bar(
            db, instrument, or_start + timedelta(minutes=i),
            open=mid, high=mid + 5, low=mid - 5, close=mid,
        )


def _seed_indicator_series(
    db: Session, instrument: Instrument, name: str, values: list[float], first_ts: datetime
) -> None:
    for i, v in enumerate(values):
        db.add(IndicatorSnapshot(
            id=uuid.uuid4(), instrument_id=instrument.id, indicator_name=name,
            timeframe=BAR_TIMEFRAME, value=v, ts=first_ts + timedelta(minutes=i),
        ))
    db.flush()


def _seed_vix(db: Session, value: float, ts: datetime) -> None:
    vix_inst = Instrument(
        id=uuid.uuid4(), symbol=VIX_SYMBOL, exchange="NSE", lot_size=1, tick_size=0.01,
    )
    db.add(vix_inst)
    db.flush()
    db.add(QuoteTick(
        id=uuid.uuid4(), instrument_id=vix_inst.id, ltp=value,
        bid=value, ask=value, volume=0, oi=None, ts=ts,
    ))
    db.flush()


OR_START = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
BREAKOUT_TS = OR_START + timedelta(minutes=OR_MINUTES)


def _bullish_breakout_bar(db: Session, instrument: Instrument) -> PriceBar:
    return _seed_bar(
        db, instrument, BREAKOUT_TS, open=22030, high=22060, low=22025, close=22050,
    )


def _bearish_breakout_bar(db: Session, instrument: Instrument, ts: datetime) -> PriceBar:
    return _seed_bar(db, instrument, ts, open=21990, high=21995, low=21950, close=21960)


class TestORBConvictionBaseline:
    def test_no_gates_enabled_fires_like_plain_orb(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)
        bar = _bullish_breakout_bar(db, instrument)

        strategy = ORBConvictionStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        proposal = strategy.check_setup(db, run, bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.payload["strategy"] == "orb_conviction"


class TestMaxTradesPerDay:
    def test_second_direction_blocked_when_cap_is_one(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)

        strategy = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, max_trades_per_day=1,
        )
        first = strategy.check_setup(db, run, _bullish_breakout_bar(db, instrument))
        second = strategy.check_setup(
            db, run, _bearish_breakout_bar(db, instrument, BREAKOUT_TS + timedelta(minutes=1)),
        )

        assert first is not None
        assert second is None

    def test_default_cap_of_two_allows_both_directions(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)

        strategy = ORBConvictionStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES)
        first = strategy.check_setup(db, run, _bullish_breakout_bar(db, instrument))
        second = strategy.check_setup(
            db, run, _bearish_breakout_bar(db, instrument, BREAKOUT_TS + timedelta(minutes=1)),
        )

        assert first is not None
        assert second is not None
        assert second.option_contract_id == option_contract_pe.id


class TestHtfEmaTrendGate:
    def test_blocked_when_ema_trend_disagrees_then_refires_when_it_agrees(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)
        # Falling EMA9, below EMA20 -> disagrees with a bullish (CE) breakout.
        _seed_indicator_series(
            db, instrument, "EMA9", [22050, 22040, 22030, 22020, 22010, 22000], OR_START,
        )
        _seed_indicator_series(db, instrument, "EMA20", [22100] * 6, OR_START)

        strategy = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, require_htf_ema_trend=True,
        )
        blocked = strategy.check_setup(db, run, _bullish_breakout_bar(db, instrument))
        assert blocked is None

        # Regime flips bullish; a later bar should now qualify (direction
        # latch was rolled back).
        _seed_indicator_series(
            db, instrument, "EMA9",
            [22110, 22120, 22130, 22140, 22150, 22160],
            BREAKOUT_TS + timedelta(minutes=1),
        )
        _seed_indicator_series(
            db, instrument, "EMA20", [22100] * 6, BREAKOUT_TS + timedelta(minutes=1),
        )
        later_bar = _seed_bar(
            db, instrument, BREAKOUT_TS + timedelta(minutes=10),
            open=22040, high=22070, low=22035, close=22060,
        )
        allowed = strategy.check_setup(db, run, later_bar)
        assert allowed is not None
        assert allowed.option_contract_id == option_contract_ce.id

    def test_allowed_when_ema_trend_agrees(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)
        _seed_indicator_series(
            db, instrument, "EMA9", [22000, 22015, 22030, 22045, 22060, 22075], OR_START,
        )
        _seed_indicator_series(db, instrument, "EMA20", [21990] * 6, OR_START)

        strategy = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, require_htf_ema_trend=True,
        )
        proposal = strategy.check_setup(db, run, _bullish_breakout_bar(db, instrument))
        assert proposal is not None

    def test_blocked_when_ema_not_warmed_up(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)

        strategy = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, require_htf_ema_trend=True,
        )
        assert strategy.check_setup(db, run, _bullish_breakout_bar(db, instrument)) is None


class TestAtrExpansionGate:
    def test_blocked_when_atr_flat(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)
        _seed_indicator_series(db, instrument, "ATR14", [10.0] * 6, OR_START)

        strategy = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES,
            require_atr_expansion=True, atr_expansion_lookback=5,
        )
        assert strategy.check_setup(db, run, _bullish_breakout_bar(db, instrument)) is None

    def test_allowed_when_atr_expanding(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)
        _seed_indicator_series(db, instrument, "ATR14", [8, 8, 9, 9, 10, 20], OR_START)

        strategy = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES,
            require_atr_expansion=True, atr_expansion_lookback=5,
        )
        assert strategy.check_setup(db, run, _bullish_breakout_bar(db, instrument)) is not None


class TestVixBandGate:
    def test_blocked_when_vix_above_max(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)
        _seed_vix(db, 32.0, OR_START + timedelta(minutes=5))

        strategy = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, vix_max=20.0,
        )
        assert strategy.check_setup(db, run, _bullish_breakout_bar(db, instrument)) is None

    def test_allowed_when_no_vix_tick_available(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)

        strategy = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, vix_min=10.0, vix_max=20.0,
        )
        assert strategy.check_setup(db, run, _bullish_breakout_bar(db, instrument)) is not None


class TestTargetRMultiple:
    def test_target_is_recomputed_from_risk_multiple(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)

        strategy = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, target_r_multiple=3.0,
        )
        proposal = strategy.check_setup(db, run, _bullish_breakout_bar(db, instrument))

        assert proposal is not None
        risk = proposal.entry_price - proposal.stop_price
        assert risk > 0
        assert proposal.target_price == pytest.approx(
            proposal.entry_price + 3.0 * risk, abs=0.05
        )


class TestBuildStrategyDispatch:
    def test_build_strategy_maps_orb_conviction_type(self, db, workspace):
        from app.api.v1.strategies import _build_strategy

        config = StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="d",
            strategy_type="orb_conviction",
            params={"require_atr_expansion": True, "vix_max": 22.0, "target_r_multiple": 2.5},
        )
        strategy = _build_strategy(config, uuid.uuid4(), EXPIRY)

        assert isinstance(strategy, ORBConvictionStrategy)
        assert strategy.require_atr_expansion is True
        assert strategy.vix_max == 22.0
        assert strategy.target_r_multiple == 2.5

    def test_build_strategy_maps_2026_08_28_batch_params(self, db, workspace):
        from app.api.v1.strategies import _build_strategy

        config = StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="d2",
            strategy_type="orb_conviction",
            params={
                "ce_only": True, "skip_weekdays": ["Tuesday"],
                "min_breakout_strength_atr": 0.5, "require_drift_alignment": True,
                "max_loss_per_lot": 2500.0, "time_stop_minutes": 90.0,
            },
        )
        s = _build_strategy(config, uuid.uuid4(), EXPIRY)
        assert isinstance(s, ORBConvictionStrategy)
        assert s.ce_only is True
        assert s.skip_weekdays == {"Tuesday"}
        assert s.min_breakout_strength_atr == 0.5
        assert s.require_drift_alignment is True
        assert s.max_loss_per_lot == 2500.0
        assert s.time_stop_minutes == 90.0


class TestFindingsDrivenGates:
    # OR_START (2026-07-24) is a Friday.
    def test_ce_only_blocks_pe_breakout(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)
        s = ORBConvictionStrategy(instrument.id, EXPIRY, or_minutes=OR_MINUTES, ce_only=True)
        assert s.check_setup(
            db, run, _bearish_breakout_bar(db, instrument, BREAKOUT_TS)
        ) is None
        # a CE breakout the next minute still goes through
        ce_bar = _seed_bar(
            db, instrument, BREAKOUT_TS + timedelta(minutes=1),
            open=22030, high=22060, low=22025, close=22050,
        )
        assert s.check_setup(db, run, ce_bar) is not None

    def test_skip_weekdays_blocks_configured_day(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)
        bar = _bullish_breakout_bar(db, instrument)
        blocked = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, skip_weekdays=["Friday"]
        )
        assert blocked.check_setup(db, run, bar) is None
        allowed = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, skip_weekdays=["Tuesday"]
        )
        assert allowed.check_setup(db, run, bar) is not None

    def test_min_breakout_strength_atr_blocks_weak_poke(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)
        _seed_indicator_series(db, instrument, "ATR14", [20.0] * 3, OR_START)
        # close 22032 is only 2 pts beyond or_high=22030; need 0.5*20 = 10.
        weak = _seed_bar(
            db, instrument, BREAKOUT_TS, open=22030, high=22033, low=22029, close=22032,
        )
        s = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, min_breakout_strength_atr=0.5,
        )
        assert s.check_setup(db, run, weak) is None

    def test_drift_alignment_blocks_counter_drift_breakout(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        # opening range built so the FIRST bar opens well ABOVE the breakout close
        # -> net drift down -> a CE (bullish) breakout is counter-drift.
        _seed_bar(db, instrument, OR_START, open=22200, high=22205, low=21990, close=22010)
        for i in range(1, OR_MINUTES):
            _seed_bar(
                db, instrument, OR_START + timedelta(minutes=i),
                open=22010, high=22030, low=21990, close=22010,
            )
        ce_bar = _seed_bar(
            db, instrument, BREAKOUT_TS, open=22030, high=22060, low=22025, close=22050,
        )
        s = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES, require_drift_alignment=True,
        )
        assert s.check_setup(db, run, ce_bar) is None

    def test_risk_overlays_are_set_on_the_proposal(
        self, db, instrument, option_contract_ce, option_contract_pe, trading_session,
        strategy_config, user,
    ):
        run = _make_strategy_run(db, strategy_config, trading_session, user, OR_START)
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_opening_range(db, instrument, OR_START, or_high=22030.0, or_low=21990.0)
        s = ORBConvictionStrategy(
            instrument.id, EXPIRY, or_minutes=OR_MINUTES,
            max_loss_per_lot=2500.0, time_stop_minutes=90.0,
        )
        p = s.check_setup(db, run, _bullish_breakout_bar(db, instrument))
        assert p is not None
        assert p.max_loss_per_lot == 2500.0
        assert p.time_stop_minutes == 90.0
