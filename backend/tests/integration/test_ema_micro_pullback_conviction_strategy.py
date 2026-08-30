"""EMAMicroPullbackConvictionStrategy.check_setup — the conviction-gate
wiring on top of EMAMicroPullbackStrategy. The gates themselves are already
exhaustively covered by `test_orb_conviction_strategy.py` against the
identical `ConvictionGateMixin` code; these tests only exercise what's new
for this subclass: the option_type DB-lookup wiring, one representative gate
firing end-to-end, and the new PCR gate.
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
from app.modules.strategy_engine.strategies.ema_micro_pullback import BODY_RATIO_LOOKBACK_BARS
from app.modules.strategy_engine.strategies.ema_micro_pullback_conviction import (
    EMAMicroPullbackConvictionStrategy,
)

EXPIRY = date(2026, 7, 30)
EMA20_BULLISH = 21950.0
BASE = datetime(2026, 7, 24, 10, 0, tzinfo=IST)  # Friday, inside the default morning window
PRIOR_DAY_TS = datetime(2026, 7, 23, 15, 29, tzinfo=IST)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(), workspace_id=workspace.id, broker_type=BrokerType.SHOONYA,
        label="emac-test-account", credentials_ref="config/credentials/shoonya.env",
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
        strike=22000, option_type=OptionType.CE, symbol="NIFTY26JUL22000CE-EMAC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def option_contract_pe(db: Session, instrument: Instrument) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY,
        strike=22000, option_type=OptionType.PE, symbol="NIFTY26JUL22000PE-EMAC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="emac-test",
        strategy_type="ema_micro_pullback_conviction",
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
    *, spot: float = 22000.0, ce_oi: int = 20000, pe_oi: int = 20000,
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


def _seed_expansion(
    db: Session, instrument: Instrument, *, ema20: float, spreads: list[float], start: datetime,
) -> None:
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
    high_ = max(open_, close_) + 1
    low_ = min(open_, close_) - 1
    for i in range(count):
        _seed_bar(
            db, instrument, start + timedelta(minutes=i),
            open=open_, high=high_, low=low_, close=close_,
        )


def _seed_bullish_baseline(db: Session, instrument: Instrument, base: datetime) -> PriceBar:
    """A fully valid bullish setup, same shape as
    test_ema_micro_pullback_strategy.py's own `_seed_bullish_baseline`,
    anchored to `base` (the entry bar) rather than the fixed module-level
    constant that file uses."""
    _seed_expansion(
        db, instrument, ema20=EMA20_BULLISH, spreads=[20.0, 35.0, 50.0],
        start=base - timedelta(minutes=10),
    )
    _seed_filler_bars(
        db, instrument, count=BODY_RATIO_LOOKBACK_BARS - 2,
        start=base - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS), open_=21900, close_=21908,
    )
    _seed_bar(
        db, instrument, base - timedelta(minutes=1),
        open=21980, high=21995, low=21960, close=21985,
    )
    return _seed_bar(
        db, instrument, base,
        open=21990, high=22015, low=21988, close=22010,
    )


class TestBaseline:
    def test_no_gates_enabled_fires_like_plain_ema_micro_pullback(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        entry_bar = _seed_bullish_baseline(db, instrument, BASE)

        strategy = EMAMicroPullbackConvictionStrategy(instrument.id, EXPIRY)
        proposal = strategy.check_setup(db, strategy_run, entry_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.payload["strategy"] == "ema_micro_pullback_conviction"


class TestPriorDayTrendGate:
    def test_ce_setup_blocked_below_prior_close(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_bar(db, instrument, PRIOR_DAY_TS, open=22200, high=22200, low=22200, close=22200)
        entry_bar = _seed_bullish_baseline(db, instrument, BASE)

        strategy = EMAMicroPullbackConvictionStrategy(
            instrument.id, EXPIRY, require_prior_day_trend=True,
        )
        assert strategy.check_setup(db, strategy_run, entry_bar) is None

    def test_blocked_even_with_a_freshly_queried_option_contract(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Regression test for a real bug found live on the backtest VM
        (2026-08-30) -- see vwap_pullback_conviction's identical test for
        the full explanation. `db.expire_all()` forces `check_setup`'s own
        `db.get(...)` to issue a real SELECT, reproducing what a real
        backtest/production session sees (a raw `str`, not `OptionType`)."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_bar(db, instrument, PRIOR_DAY_TS, open=22200, high=22200, low=22200, close=22200)
        entry_bar = _seed_bullish_baseline(db, instrument, BASE)
        db.expire_all()

        strategy = EMAMicroPullbackConvictionStrategy(
            instrument.id, EXPIRY, require_prior_day_trend=True,
        )
        assert strategy.check_setup(db, strategy_run, entry_bar) is None

    def test_ce_setup_allowed_above_prior_close(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_bar(db, instrument, PRIOR_DAY_TS, open=21800, high=21800, low=21800, close=21800)
        entry_bar = _seed_bullish_baseline(db, instrument, BASE)

        strategy = EMAMicroPullbackConvictionStrategy(
            instrument.id, EXPIRY, require_prior_day_trend=True,
        )
        proposal = strategy.check_setup(db, strategy_run, entry_bar)
        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id


class TestPcrGate:
    def test_blocked_when_pcr_below_min(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(
            db, instrument, option_contract_ce, option_contract_pe, ce_oi=50000, pe_oi=5000
        )
        entry_bar = _seed_bullish_baseline(db, instrument, BASE)

        strategy = EMAMicroPullbackConvictionStrategy(instrument.id, EXPIRY, pcr_oi_min=0.4)
        assert strategy.check_setup(db, strategy_run, entry_bar) is None

    def test_allowed_when_pcr_within_band(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(
            db, instrument, option_contract_ce, option_contract_pe, ce_oi=20000, pe_oi=20000
        )
        entry_bar = _seed_bullish_baseline(db, instrument, BASE)

        strategy = EMAMicroPullbackConvictionStrategy(
            instrument.id, EXPIRY, pcr_oi_min=0.4, pcr_oi_max=2.0,
        )
        assert strategy.check_setup(db, strategy_run, entry_bar) is not None


class TestBuildStrategyDispatch:
    def test_build_strategy_maps_ema_micro_pullback_conviction_type(self, db, workspace):
        from app.api.v1.strategies import _build_strategy

        config = StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="d",
            strategy_type="ema_micro_pullback_conviction",
            params={
                "require_prior_day_trend": True, "pcr_oi_min": 0.4,
                "min_body_ratio": 0.55,
            },
        )
        strategy = _build_strategy(config, uuid.uuid4(), EXPIRY)

        assert isinstance(strategy, EMAMicroPullbackConvictionStrategy)
        assert strategy.require_prior_day_trend is True
        assert strategy.pcr_oi_min == 0.4
        assert strategy.min_body_ratio == 0.55
