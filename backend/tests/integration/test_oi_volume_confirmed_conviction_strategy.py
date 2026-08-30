"""OIVolumeConfirmedConvictionStrategy.check_setup — the conviction-gate
wiring on top of OIVolumeConfirmedStrategy. The gates themselves are already
exhaustively covered by `test_orb_conviction_strategy.py` against the
identical `ConvictionGateMixin` code; these tests exercise what's new for
this subclass: the option_type DB-lookup wiring, the new PCR gate, and —
unlike VWAP/EMA — the `_fired_directions.discard()` undo, since
`OIVolumeConfirmedStrategy.check_setup` latches a direction *before*
returning the proposal this subclass's gate then evaluates.
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
from app.modules.strategy_engine.strategies.oi_volume_confirmed import BODY_RATIO_LOOKBACK_BARS
from app.modules.strategy_engine.strategies.oi_volume_confirmed_conviction import (
    OIVolumeConfirmedConvictionStrategy,
)

EXPIRY = date(2026, 7, 30)
LOOKBACK_BARS = 5
# Default NIFTY range filter is [15, 60] -- comfortably inside it.
WINDOW_HIGH = 22030.0
WINDOW_LOW = 21990.0
BASE = datetime(2026, 7, 24, 10, 0, tzinfo=IST)  # Friday, inside the default morning window
PRIOR_DAY_TS = datetime(2026, 7, 23, 15, 29, tzinfo=IST)
FILLER_BARS = BODY_RATIO_LOOKBACK_BARS - LOOKBACK_BARS - 1


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(), workspace_id=workspace.id, broker_type=BrokerType.SHOONYA,
        label="oic-test-account", credentials_ref="config/credentials/shoonya.env",
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
        strike=22000, option_type=OptionType.CE, symbol="NIFTY26JUL22000CE-OIC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def option_contract_pe(db: Session, instrument: Instrument) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY,
        strike=22000, option_type=OptionType.PE, symbol="NIFTY26JUL22000PE-OIC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="oic-test",
        strategy_type="oi_volume_confirmed_conviction",
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


def _seed_window_and_filler(
    db: Session, instrument: Instrument, start: datetime,
    *, window_high: float = WINDOW_HIGH, window_low: float = WINDOW_LOW,
    filler_price: float = 21900,
) -> None:
    for i in range(FILLER_BARS):
        _seed_bar(
            db, instrument, start + timedelta(minutes=i),
            open=filler_price, high=filler_price + 9, low=filler_price - 1,
            close=filler_price + 8,
        )
    mid = (window_high + window_low) / 2
    q = (window_high - window_low) / 4
    specs = [
        (mid, window_high, mid - q, mid + q),
        (mid, mid + q, window_low, mid - q),
        (mid - q, mid + q, mid - q, mid + q),
        (mid + q, mid + q, mid - q, mid - q),
        (mid - q, mid + q, mid - q, mid + q),
    ]
    for i, (o, h, low, c) in enumerate(specs[:LOOKBACK_BARS]):
        _seed_bar(
            db, instrument, start + timedelta(minutes=FILLER_BARS + i),
            open=o, high=h, low=low, close=c,
        )


def _seed_breakout_setup(
    db: Session, instrument: Instrument, base: datetime,
    *, window_high: float = WINDOW_HIGH, window_low: float = WINDOW_LOW,
    filler_price: float = 21900,
) -> PriceBar:
    _seed_window_and_filler(
        db, instrument, base - timedelta(minutes=BODY_RATIO_LOOKBACK_BARS),
        window_high=window_high, window_low=window_low, filler_price=filler_price,
    )
    breakout_close = window_high + 20
    return _seed_bar(
        db, instrument, base,
        open=window_high, high=breakout_close + 5, low=window_high - 2, close=breakout_close,
    )


class TestBaseline:
    def test_no_gates_enabled_fires_like_plain_oi_volume_confirmed(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        breakout_bar = _seed_breakout_setup(db, instrument, BASE)

        strategy = OIVolumeConfirmedConvictionStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS
        )
        proposal = strategy.check_setup(db, strategy_run, breakout_bar)

        assert proposal is not None
        assert proposal.option_contract_id == option_contract_ce.id
        assert proposal.payload["strategy"] == "oi_volume_confirmed_conviction"


class TestPriorDayTrendGateUndoesLatch:
    def test_ce_breakout_blocked_below_prior_close_then_refires_above_it(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """The core behavior orb_conviction.py's own docstring warns about:
        without the `_fired_directions.discard()` undo, a conviction-
        rejected breakout would permanently block that direction for the
        rest of the run, since the base class already latched it before
        this subclass's gate ever ran."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_bar(db, instrument, PRIOR_DAY_TS, open=22200, high=22200, low=22200, close=22200)
        breakout_bar = _seed_breakout_setup(db, instrument, BASE)

        strategy = OIVolumeConfirmedConvictionStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS, require_prior_day_trend=True,
        )
        blocked = strategy.check_setup(db, strategy_run, breakout_bar)
        assert blocked is None
        assert OptionType.CE not in strategy._fired_directions

        # A second, independent breakout episode well after the first (its
        # own fresh window+filler, far enough ahead that the rolling
        # last-10-bars window no longer includes the first breakout bar),
        # this time above the prior-day close, should now qualify -- proving
        # the latch was rolled back, not left permanently blocking CE.
        later_base = BASE + timedelta(minutes=30)
        later_breakout = _seed_breakout_setup(
            db, instrument, later_base, window_high=22250.0, window_low=22210.0,
            filler_price=22150,
        )
        allowed = strategy.check_setup(db, strategy_run, later_breakout)
        assert allowed is not None
        assert allowed.option_contract_id == option_contract_ce.id

    def test_blocked_even_with_a_freshly_queried_option_contract(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        """Regression test for a real bug found live on the backtest VM
        (2026-08-30) -- see vwap_pullback_conviction's identical test for
        the full explanation. `db.expire_all()` forces `check_setup`'s own
        `db.get(...)` to issue a real SELECT, reproducing what a real
        backtest/production session sees (a raw `str`, not `OptionType`) --
        the `not in strategy._fired_directions` check alone wasn't enough to
        catch this (a `StrEnum` hashes/compares equal to its own string
        value, so set membership still worked even with the bug present);
        only the actual `is OptionType.CE`/`is OptionType.PE` gate checks
        were broken."""
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe)
        _seed_bar(db, instrument, PRIOR_DAY_TS, open=22200, high=22200, low=22200, close=22200)
        breakout_bar = _seed_breakout_setup(db, instrument, BASE)
        db.expire_all()

        strategy = OIVolumeConfirmedConvictionStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS, require_prior_day_trend=True,
        )
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None


class TestPcrGate:
    def test_blocked_when_pcr_below_min(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(db, instrument, option_contract_ce, option_contract_pe, ce_oi=50000, pe_oi=5000)
        breakout_bar = _seed_breakout_setup(db, instrument, BASE)

        strategy = OIVolumeConfirmedConvictionStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS, pcr_oi_min=0.4,
        )
        assert strategy.check_setup(db, strategy_run, breakout_bar) is None

    def test_allowed_when_pcr_within_band(
        self, db, instrument, option_contract_ce, option_contract_pe, strategy_run,
    ):
        _seed_chain(
            db, instrument, option_contract_ce, option_contract_pe, ce_oi=20000, pe_oi=20000
        )
        breakout_bar = _seed_breakout_setup(db, instrument, BASE)

        strategy = OIVolumeConfirmedConvictionStrategy(
            instrument.id, EXPIRY, lookback_bars=LOOKBACK_BARS, pcr_oi_min=0.4, pcr_oi_max=2.0,
        )
        assert strategy.check_setup(db, strategy_run, breakout_bar) is not None


class TestBuildStrategyDispatch:
    def test_build_strategy_maps_oi_volume_confirmed_conviction_type(self, db, workspace):
        from app.api.v1.strategies import _build_strategy

        config = StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="d",
            strategy_type="oi_volume_confirmed_conviction",
            params={
                "require_prior_day_trend": True, "pcr_oi_min": 0.4,
                "oi_use_futures_volume_confirmation": False,
            },
        )
        strategy = _build_strategy(config, uuid.uuid4(), EXPIRY)

        assert isinstance(strategy, OIVolumeConfirmedConvictionStrategy)
        assert strategy.require_prior_day_trend is True
        assert strategy.pcr_oi_min == 0.4
