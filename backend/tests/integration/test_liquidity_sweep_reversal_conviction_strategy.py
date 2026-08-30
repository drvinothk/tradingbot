"""LiquiditySweepReversalConvictionStrategy.check_setup — the conviction-gate
wiring on top of LiquiditySweepReversalStrategy. The gates themselves are
already exhaustively covered by `test_orb_conviction_strategy.py` against
the identical `ConvictionGateMixin` code; these tests exercise what's new
for this subclass: the option_type DB-lookup wiring (only meaningful at the
*confirmation* bar — the sweep-detection bar always returns `None` from the
base class before any gate runs), one representative gate firing end-to-end,
and the new PCR gate.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

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
from app.domain.market.models import QuoteTick as QuoteTickRow
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import ExecutionMode, StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.strategy_engine.common_rules import BAR_TIMEFRAME
from app.modules.strategy_engine.strategies.liquidity_sweep_reversal_conviction import (
    LiquiditySweepReversalConvictionStrategy,
)

EXPIRY = date(2026, 7, 30)
LOOKBACK_BARS = 10
# Default NIFTY range filter is [30, 120] -- 100 is comfortably inside it.
WINDOW_HIGH = 22050.0
WINDOW_LOW = 21950.0
BASE = datetime(2026, 7, 24, 10, 0, tzinfo=IST)  # Friday, inside the default morning window
PRIOR_DAY_TS = datetime(2026, 7, 23, 15, 29, tzinfo=IST)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(), workspace_id=workspace.id, broker_type=BrokerType.SHOONYA,
        label="sweepc-test-account", credentials_ref="config/credentials/shoonya.env",
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
        strike=22000, option_type=OptionType.CE, symbol="NIFTY26JUL22000CE-SWEEPC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def option_contract_pe(db: Session, instrument: Instrument) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY,
        strike=22000, option_type=OptionType.PE, symbol="NIFTY26JUL22000PE-SWEEPC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="sweepc-test",
        strategy_type="liquidity_sweep_reversal_conviction",
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


def _seed_window(
    db: Session, instrument: Instrument, start: datetime,
    *, window_high: float, window_low: float,
) -> None:
    mid = (window_high + window_low) / 2
    q = (window_high - window_low) / 4
    specs = [
        (mid, window_high, mid - q, mid + q),
        (mid, mid + q, window_low, mid - q),
        (mid - q, mid + q, mid - q, mid + q),
        (mid + q, mid + q, mid - q, mid - q),
        (mid - q, mid + q, mid - q, mid + q),
    ]
    for i in range(LOOKBACK_BARS):
        o, h, low, c = specs[i % len(specs)]
        _seed_bar(db, instrument, start + timedelta(minutes=i), open=o, high=h, low=low, close=c)


def _seed_bullish_sweep(
    db: Session, instrument: Instrument, *,
    window_high: float = WINDOW_HIGH, window_low: float = WINDOW_LOW, base: datetime = BASE,
) -> PriceBar:
    start = base - timedelta(minutes=LOOKBACK_BARS + 2)
    _seed_window(db, instrument, start, window_high=window_high, window_low=window_low)
    return _seed_bar(
        db, instrument, base - timedelta(minutes=1),
        open=window_low + 20, high=window_low + 30, low=window_low - 15, close=window_low + 10,
    )


def _seed_bullish_confirmation(
    db: Session, instrument: Instrument, sweep_bar: PriceBar, *, base: datetime = BASE,
) -> PriceBar:
    return _seed_bar(
        db, instrument, base,
        open=float(sweep_bar.high) + 5, high=float(sweep_bar.high) + 30,
        low=float(sweep_bar.high), close=float(sweep_bar.high) + 25,
    )


class TestBaseline:
    def test_no_gates_enabled_fires_like_plain_liquidity_sweep_reversal(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        strategy = LiquiditySweepReversalConvictionStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS,
        )
        sweep_bar = _seed_bullish_sweep(db, instrument)
        assert strategy.check_setup(db, strategy_run, sweep_bar) is None  # deferred to next bar

        confirmation = _seed_bullish_confirmation(db, instrument, sweep_bar)
        proposal = strategy.check_setup(db, strategy_run, confirmation)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.payload["strategy"] == "liquidity_sweep_reversal_conviction"


class TestPriorDayTrendGate:
    def test_ce_reversal_blocked_below_prior_close_then_allowed_on_a_later_episode(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_bar(db, instrument, PRIOR_DAY_TS, open=22200, high=22200, low=22200, close=22200)

        strategy = LiquiditySweepReversalConvictionStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS, require_prior_day_trend=True,
        )
        sweep_bar = _seed_bullish_sweep(db, instrument)
        strategy.check_setup(db, strategy_run, sweep_bar)
        confirmation = _seed_bullish_confirmation(db, instrument, sweep_bar)
        # confirmation close ~ sweep_bar.high + 25 ~ 21995, below prior close 22200.
        assert strategy.check_setup(db, strategy_run, confirmation) is None

        # A second, independent sweep+confirmation episode well after the
        # first, at a window comfortably above the prior close, should now
        # qualify -- `LiquiditySweepReversalStrategy` doesn't latch a
        # direction, so no discard() is needed (unlike OI/Volume Confirmed),
        # but a fresh episode still needs its own valid rolling window.
        later_base = BASE + timedelta(minutes=30)
        later_sweep = _seed_bullish_sweep(
            db, instrument, window_high=22320.0, window_low=22220.0, base=later_base,
        )
        strategy.check_setup(db, strategy_run, later_sweep)
        later_confirmation = _seed_bullish_confirmation(
            db, instrument, later_sweep, base=later_base
        )
        allowed = strategy.check_setup(db, strategy_run, later_confirmation)
        assert allowed is not None
        assert allowed.option_contract_id == option_contract_ce.id

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
        strategy = LiquiditySweepReversalConvictionStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS, require_prior_day_trend=True,
        )
        sweep_bar = _seed_bullish_sweep(db, instrument)
        strategy.check_setup(db, strategy_run, sweep_bar)
        confirmation = _seed_bullish_confirmation(db, instrument, sweep_bar)
        db.expire_all()
        assert strategy.check_setup(db, strategy_run, confirmation) is None


class TestPcrGate:
    def test_blocked_when_pcr_below_min(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe, ce_oi=50000, pe_oi=5000)
        strategy = LiquiditySweepReversalConvictionStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS, pcr_oi_min=0.4,
        )
        sweep_bar = _seed_bullish_sweep(db, instrument)
        strategy.check_setup(db, strategy_run, sweep_bar)
        confirmation = _seed_bullish_confirmation(db, instrument, sweep_bar)
        assert strategy.check_setup(db, strategy_run, confirmation) is None

    def test_allowed_when_pcr_within_band(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(
            db, instrument, option_contract_ce, option_contract_pe, ce_oi=20000, pe_oi=20000
        )
        strategy = LiquiditySweepReversalConvictionStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS, pcr_oi_min=0.4, pcr_oi_max=2.0,
        )
        sweep_bar = _seed_bullish_sweep(db, instrument)
        strategy.check_setup(db, strategy_run, sweep_bar)
        confirmation = _seed_bullish_confirmation(db, instrument, sweep_bar)
        assert strategy.check_setup(db, strategy_run, confirmation) is not None


class TestBuildStrategyDispatch:
    def test_build_strategy_maps_liquidity_sweep_reversal_conviction_type(self, db, workspace):
        from app.api.v1.strategies import _build_strategy

        config = StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="d",
            strategy_type="liquidity_sweep_reversal_conviction",
            params={"require_prior_day_trend": True, "pcr_oi_min": 0.4, "min_body_ratio": 0.5},
        )
        strategy = _build_strategy(config, uuid.uuid4(), EXPIRY)

        assert isinstance(strategy, LiquiditySweepReversalConvictionStrategy)
        assert strategy.require_prior_day_trend is True
        assert strategy.pcr_oi_min == 0.4
        assert strategy.min_body_ratio == 0.5
