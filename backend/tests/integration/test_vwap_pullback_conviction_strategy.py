"""VWAPPullbackConvictionStrategy.check_setup — the conviction-gate wiring
on top of VWAPPullbackStrategy. The gates themselves (prior-day trend, VIX,
ATR expansion, volume surge, HTF EMA) are already exhaustively covered by
`test_orb_conviction_strategy.py` against the identical
`ConvictionGateMixin` code; these tests only exercise what's new for this
subclass: the option_type DB-lookup wiring, one representative gate firing
end-to-end, and the new PCR gate (which has no coverage anywhere else).
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
from app.modules.strategy_engine.strategies.vwap_pullback_conviction import (
    VWAPPullbackConvictionStrategy,
)

EXPIRY = date(2026, 7, 30)
VWAP = 22000.0
BASE = datetime(2026, 7, 24, 10, 0, tzinfo=IST)  # Friday
PRIOR_DAY_TS = datetime(2026, 7, 23, 15, 29, tzinfo=IST)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(), workspace_id=workspace.id, broker_type=BrokerType.SHOONYA,
        label="vwapc-test-account", credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    return account


@pytest.fixture
def trading_session(db: Session, workspace, broker_account, user: User) -> TradingSession:
    ts = TradingSession(
        id=uuid.uuid4(), workspace_id=workspace.id, broker_account_id=broker_account.id,
        started_by_user_id=user.id, mode=SafeMode.PAPER_ONLY, started_at=datetime.now(IST),
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
    contract = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY,
        strike=22000, option_type=OptionType.CE, symbol="NIFTY26JUL22000CE-VWAPC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def option_contract_pe(db: Session, instrument: Instrument) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY,
        strike=22000, option_type=OptionType.PE, symbol="NIFTY26JUL22000PE-VWAPC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="vwapc-test",
        strategy_type="vwap_pullback_conviction",
    )
    db.add(config)
    db.flush()
    return config


@pytest.fixture
def strategy_run(
    db: Session, strategy_config: StrategyConfig, trading_session, user: User
) -> StrategyRun:
    run = StrategyRun(
        id=uuid.uuid4(), strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id, execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING, started_at=datetime.now(IST),
        started_by_user_id=user.id,
    )
    db.add(run)
    db.flush()
    return run


def _seed_chain(
    db: Session, instrument: Instrument, ce: OptionContract, pe: OptionContract,
    *, spot: float = VWAP, ce_oi: int = 20000, pe_oi: int = 20000,
) -> None:
    now = datetime.now(IST)
    db.add(QuoteTickRow(
        id=uuid.uuid4(), instrument_id=instrument.id, ltp=spot,
        bid=spot - 1, ask=spot + 1, volume=10000, oi=None, ts=now,
    ))
    db.add(OptionChainSnapshot(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY, ts=now,
        chain_data=[
            {
                "contract_symbol": ce.symbol, "strike": float(ce.strike),
                "option_type": OptionType.CE.value, "ltp": 80.0,
                "bid": 79.5, "ask": 80.5, "volume": 5000, "oi": ce_oi,
            },
            {
                "contract_symbol": pe.symbol, "strike": float(pe.strike),
                "option_type": OptionType.PE.value, "ltp": 75.0,
                "bid": 74.5, "ask": 75.5, "volume": 5000, "oi": pe_oi,
            },
        ],
    ))
    db.flush()


def _seed_vwap(db: Session, instrument: Instrument, value: float = VWAP) -> None:
    db.add(IndicatorSnapshot(
        id=uuid.uuid4(), instrument_id=instrument.id, indicator_name="VWAP",
        timeframe=BAR_TIMEFRAME, value=value, ts=datetime.now(IST),
    ))
    db.flush()


def _seed_bar(
    db: Session, instrument: Instrument, bucket_start: datetime,
    *, open: float, high: float, low: float, close: float,  # noqa: A002
) -> PriceBar:
    bar = PriceBar(
        id=uuid.uuid4(), instrument_id=instrument.id, timeframe=BAR_TIMEFRAME,
        bucket_start=bucket_start, open=open, high=high, low=low, close=close, volume=1000,
    )
    db.add(bar)
    db.flush()
    return bar


def _seed_trending_history(db: Session, instrument: Instrument, base: datetime, side: str) -> None:
    price = VWAP + 20.0 if side == "bullish" else VWAP - 20.0
    for i in range(18, 0, -1):
        _seed_bar(
            db, instrument, base - timedelta(minutes=i),
            open=price, high=price + 5, low=price - 5, close=price,
        )


def _seed_bullish_confirmation(db: Session, instrument: Instrument, base: datetime) -> PriceBar:
    _seed_vwap(db, instrument, VWAP)
    _seed_trending_history(db, instrument, base, "bullish")
    _seed_bar(db, instrument, base, open=22015, high=22020, low=VWAP, close=22010)
    return _seed_bar(
        db, instrument, base + timedelta(minutes=1),
        open=22015, high=22035, low=22012, close=22030,
    )


class TestBaseline:
    def test_no_gates_enabled_fires_like_plain_vwap_pullback(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        confirmation = _seed_bullish_confirmation(db, instrument, BASE)

        strategy = VWAPPullbackConvictionStrategy(instrument.id, EXPIRY)
        proposal = strategy.check_setup(db, strategy_run, confirmation)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.payload["strategy"] == "vwap_pullback_conviction"


class TestPriorDayTrendGate:
    def test_ce_setup_blocked_below_prior_close(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        # Prior close well above the confirmation bar's close (22030).
        _seed_bar(db, instrument, PRIOR_DAY_TS, open=22200, high=22200, low=22200, close=22200)
        confirmation = _seed_bullish_confirmation(db, instrument, BASE)

        strategy = VWAPPullbackConvictionStrategy(
            instrument.id, EXPIRY, require_prior_day_trend=True,
        )
        assert strategy.check_setup(db, strategy_run, confirmation) is None

    def test_blocked_even_with_a_freshly_queried_option_contract(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Regression test for a real bug found live on the backtest VM
        (2026-08-30): `OptionContract.option_type` is `Mapped[OptionType]`
        but backed by a plain `String(2)` column (no SQLAlchemy Enum type),
        so a genuinely fresh SELECT (not an identity-map hit) returns a raw
        `str`, not an `OptionType` member. Every other test in this file
        reads `option_contract_ce`/`option_contract_pe` straight out of the
        same session's identity map (never expired), which masked this —
        `db.expire_all()` here forces `check_setup`'s own `db.get(...)` to
        issue a real SELECT, reproducing exactly what a real backtest/
        production session sees."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_bar(db, instrument, PRIOR_DAY_TS, open=22200, high=22200, low=22200, close=22200)
        confirmation = _seed_bullish_confirmation(db, instrument, BASE)
        db.expire_all()

        strategy = VWAPPullbackConvictionStrategy(
            instrument.id, EXPIRY, require_prior_day_trend=True,
        )
        assert strategy.check_setup(db, strategy_run, confirmation) is None

    def test_ce_setup_allowed_above_prior_close(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_bar(db, instrument, PRIOR_DAY_TS, open=21800, high=21800, low=21800, close=21800)
        confirmation = _seed_bullish_confirmation(db, instrument, BASE)

        strategy = VWAPPullbackConvictionStrategy(
            instrument.id, EXPIRY, require_prior_day_trend=True,
        )
        proposal = strategy.check_setup(db, strategy_run, confirmation)
        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id


class TestPcrGate:
    def test_blocked_when_pcr_below_min(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        # pcr_oi = put_oi / call_oi = 5000 / 50000 = 0.1
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe, ce_oi=50000, pe_oi=5000)
        confirmation = _seed_bullish_confirmation(db, instrument, BASE)

        strategy = VWAPPullbackConvictionStrategy(instrument.id, EXPIRY, pcr_oi_min=0.4)
        assert strategy.check_setup(db, strategy_run, confirmation) is None

    def test_allowed_when_pcr_within_band(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        # pcr_oi = 20000 / 20000 = 1.0
        _seed_chain(
            db, instrument, option_contract_ce, option_contract_pe, ce_oi=20000, pe_oi=20000
        )
        confirmation = _seed_bullish_confirmation(db, instrument, BASE)

        strategy = VWAPPullbackConvictionStrategy(
            instrument.id, EXPIRY, pcr_oi_min=0.4, pcr_oi_max=2.0,
        )
        proposal = strategy.check_setup(db, strategy_run, confirmation)
        assert proposal is not None

    def test_allowed_when_no_chain_snapshot_yet(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Missing PCR data isn't treated as an adverse regime -- same
        convention the VIX gate already established."""
        confirmation = _seed_bullish_confirmation(db, instrument, BASE)
        # Seed the chain only now, right before check_setup, so ranking can
        # still resolve a contract -- but env_metrics reads the *latest*
        # snapshot as of "now", so this still exercises a real chain.
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)

        strategy = VWAPPullbackConvictionStrategy(instrument.id, EXPIRY, pcr_oi_min=0.4)
        proposal = strategy.check_setup(db, strategy_run, confirmation)
        assert proposal is not None


class TestBuildStrategyDispatch:
    def test_build_strategy_maps_vwap_pullback_conviction_type(self, db, workspace):
        from app.api.v1.strategies import _build_strategy

        config = StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="d",
            strategy_type="vwap_pullback_conviction",
            params={
                "require_prior_day_trend": True, "pcr_oi_min": 0.4, "pcr_oi_max": 2.5,
                "min_trend_side_fraction": 0.8,
            },
        )
        strategy = _build_strategy(config, uuid.uuid4(), EXPIRY)

        assert isinstance(strategy, VWAPPullbackConvictionStrategy)
        assert strategy.require_prior_day_trend is True
        assert strategy.pcr_oi_min == 0.4
        assert strategy.pcr_oi_max == 2.5
        assert strategy.min_trend_side_fraction == 0.8
