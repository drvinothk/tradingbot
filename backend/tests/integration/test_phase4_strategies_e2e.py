"""End-to-end proof of Phase 4's "done when" (ORB, VWAP Pullback, EMA
Micro-pullback), extended in Phase 7 to also cover OI/Volume Confirmed and
Liquidity Sweep/Reversal per *that* phase's own "done when": all five run
through the real `run_cycle` (evaluate -> submit_signal -> status-refresh —
the same function `StrategyRunner`'s background thread calls every poll,
just driven directly here instead of via a real thread) through the
unchanged Signal -> TradeIntent -> RiskDecision -> dispatch pipeline,
across more than one trading_session, in a mix of auto and
approval-required execution mode, with no regression to the original three.

**2026-08-13: rewritten off real `StrategyRunner` background threads.** The
original version raced 5 real threads polling every 0.05s against a 0.4s
wall-clock window — genuinely flaky (confirmed: it fails intermittently
even on an unmodified checkout, nothing to do with any one strategy's own
logic — real thread-scheduling jitter under host load can starve a runner
past the assertion window). `run_cycle` is exposed standalone specifically
so tests can drive it deterministically (see its own docstring) — each
strategy's `evaluate()`/`check_setup()` reads already-fully-seeded bar/chain
data, so a single direct `run_cycle` call per strategy_run reaches the same
end state real polling would eventually converge to, with zero timing
dependency. Real background-thread execution (start/stop lifecycle,
timer-driven polling) is already covered elsewhere, for one strategy, by
`test_synthetic_strategy.py::test_runner_executes_on_a_timer_and_stops_cleanly`
— this file's own value was always the five-strategy pipeline proof, not
thread-safety, so nothing is lost by removing the threads here.

Uses the standard rolled-back `db`/`workspace`/`user` fixtures now (no
special real-commit factory or manual FK-safe cleanup needed — everything
happens in one transaction that rolls back automatically, same as every
other integration test in this codebase).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

import app.modules.strategy_engine.runner as runner_module
from app.core.clock import IST
from app.domain.execution.models import Position, PositionStatus
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType
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
from app.domain.strategy.models import (
    ApprovalStatus,
    ExecutionMode,
    PendingTradeApproval,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.strategy_engine.common_rules import BAR_TIMEFRAME
from app.modules.strategy_engine.runner import run_cycle
from app.modules.strategy_engine.strategies.ema_micro_pullback import EMAMicroPullbackStrategy
from app.modules.strategy_engine.strategies.liquidity_sweep_reversal import (
    LiquiditySweepReversalStrategy,
)
from app.modules.strategy_engine.strategies.oi_volume_confirmed import OIVolumeConfirmedStrategy
from app.modules.strategy_engine.strategies.orb import ORBStrategy
from app.modules.strategy_engine.strategies.vwap_pullback import VWAPPullbackStrategy

EXPIRY = date(2026, 7, 30)


@pytest.fixture(autouse=True)
def _fixed_trade_window_clock(monkeypatch):
    """This file drives `run_cycle` directly with real signal-dispatch
    assertions, without regard to wall-clock time -- pin `now_ist()` inside
    the 09:31-15:09 IST trade-firing window (runner.TRADE_WINDOW_START/END)
    so those assertions don't depend on what time of day the suite runs.
    """
    monkeypatch.setattr(
        runner_module, "now_ist", lambda: datetime(2026, 1, 1, 11, 0, tzinfo=IST)
    )


def _seed_chain(
    db, instrument: Instrument, ce: OptionContract, pe: OptionContract, *, spot: float
) -> None:
    now = datetime.now(UTC)
    db.add(
        QuoteTick(
            id=uuid.uuid4(), instrument_id=instrument.id, ltp=spot, bid=spot - 1, ask=spot + 1,
            volume=10000, oi=None, ts=now,
        )
    )
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY, ts=now,
            chain_data=[
                {
                    "contract_symbol": ce.symbol, "strike": float(ce.strike),
                    "option_type": OptionType.CE.value, "ltp": 80.0, "bid": 79.5, "ask": 80.5,
                    "volume": 5000, "oi": 20000,
                },
                {
                    "contract_symbol": pe.symbol, "strike": float(pe.strike),
                    "option_type": OptionType.PE.value, "ltp": 75.0, "bid": 74.5, "ask": 75.5,
                    "volume": 5000, "oi": 20000,
                },
            ],
        )
    )


def _seed_bar(db, instrument: Instrument, bucket_start: datetime, *, o, h, l, c) -> None:  # noqa: E741
    db.add(
        PriceBar(
            id=uuid.uuid4(), instrument_id=instrument.id, timeframe=BAR_TIMEFRAME,
            bucket_start=bucket_start, open=o, high=h, low=l, close=c, volume=1000,
        )
    )


def _seed_indicator(db, instrument: Instrument, name: str, value: float) -> None:
    db.add(
        IndicatorSnapshot(
            id=uuid.uuid4(), instrument_id=instrument.id, indicator_name=name,
            timeframe=BAR_TIMEFRAME, value=value, ts=datetime.now(UTC),
        )
    )


def _seed_orb_breakout(db, instrument: Instrument) -> None:
    """OR window flat at [21990, 22030] (width 40 -- inside ORBStrategy's
    default NIFTY range filter of 20-80 points), anchored to the fixed 9:15
    IST session open (not `strategy_run.started_at`, which is real
    wall-clock `now()` in this test -- deliberately different, proving the
    anchor is restart-independent), then a bar closing above it."""
    or_start = datetime(2026, 7, 24, 9, 15, tzinfo=IST)
    mid = 22010.0
    db.add(PriceBar(
        id=uuid.uuid4(), instrument_id=instrument.id, timeframe=BAR_TIMEFRAME,
        bucket_start=or_start, open=mid, high=22030.0, low=21990.0, close=mid, volume=1000,
    ))
    for i in range(1, 15):
        db.add(PriceBar(
            id=uuid.uuid4(), instrument_id=instrument.id, timeframe=BAR_TIMEFRAME,
            bucket_start=or_start + timedelta(minutes=i),
            open=mid, high=mid + 5, low=mid - 5, close=mid, volume=1000,
        ))
    _seed_bar(
        db, instrument, or_start + timedelta(minutes=15),
        o=22050, h=22080, l=22045, c=22070,
    )


def _seed_vwap_pullback(db, instrument: Instrument) -> None:
    """18 bars of consistent bullish trend history ahead of the pullback +
    confirmation bars -- VWAPPullbackStrategy's trend/choppiness filter
    (trend_lookback_bars=20 default) needs that much history or it blocks
    every setup as "insufficient history," same trap this session's own
    test_vwap_pullback_strategy.py hit for the identical reason.
    """
    vwap = 22000.0
    _seed_indicator(db, instrument, "VWAP", vwap)
    base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
    for i in range(18, 0, -1):
        _seed_bar(
            db, instrument, base - timedelta(minutes=i),
            o=vwap + 20, h=vwap + 25, l=vwap + 15, c=vwap + 20,
        )
    _seed_bar(db, instrument, base, o=22015, h=22020, l=vwap, c=22010)
    _seed_bar(db, instrument, base + timedelta(minutes=1), o=22015, h=22035, l=22012, c=22030)


def _seed_ema_pullback(db, instrument: Instrument) -> None:
    ema9 = 22000.0
    _seed_indicator(db, instrument, "EMA9", ema9)
    _seed_indicator(db, instrument, "EMA20", 21950.0)
    base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
    _seed_bar(db, instrument, base, o=22010, h=22015, l=ema9, c=22005)
    _seed_bar(db, instrument, base + timedelta(minutes=1), o=22010, h=22030, l=22008, c=22025)


def _seed_oivol_breakout(db, instrument: Instrument) -> None:
    """5-bar rolling window flat at [21950, 22050] (OIVolumeConfirmedStrategy's
    default lookback_bars=5), then a bar closing above it."""
    base = datetime(2026, 7, 24, 11, 0, tzinfo=UTC)
    mid = 22000.0
    _seed_bar(db, instrument, base, o=mid, h=22050.0, l=mid - 5, c=mid)
    _seed_bar(db, instrument, base + timedelta(minutes=1), o=mid, h=mid + 5, l=21950.0, c=mid)
    for i in range(2, 5):
        _seed_bar(db, instrument, base + timedelta(minutes=i), o=mid, h=mid + 5, l=mid - 5, c=mid)
    _seed_bar(db, instrument, base + timedelta(minutes=5), o=22050, h=22080, l=22045, c=22070)


def _seed_liquidity_sweep(db, instrument: Instrument) -> None:
    """10-bar rolling window flat at [21950, 22050]
    (LiquiditySweepReversalStrategy's default lookback_bars=10), then a bar
    that sweeps the window high but closes back inside it -> bearish
    reversal (PE)."""
    base = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    mid = 22000.0
    _seed_bar(db, instrument, base, o=mid, h=22050.0, l=mid - 5, c=mid)
    _seed_bar(db, instrument, base + timedelta(minutes=1), o=mid, h=mid + 5, l=21950.0, c=mid)
    for i in range(2, 10):
        _seed_bar(db, instrument, base + timedelta(minutes=i), o=mid, h=mid + 5, l=mid - 5, c=mid)
    _seed_bar(db, instrument, base + timedelta(minutes=10), o=22030, h=22070, l=22020, c=22040)


def test_five_strategies_run_concurrently_across_two_sessions_mixed_modes(
    db, workspace, user,
):
    broker_account = BrokerAccount(
        id=uuid.uuid4(), workspace_id=workspace.id, broker_type=BrokerType.SHOONYA,
        label="phase4-e2e-account", credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(broker_account)
    db.flush()

    session_a = TradingSession(
        id=uuid.uuid4(), workspace_id=workspace.id, broker_account_id=broker_account.id,
        started_by_user_id=user.id, mode=SafeMode.PAPER_ONLY, started_at=datetime.now(UTC),
        budget_amount=1_000_000, daily_target_profit=1_000_000, daily_loss_cap=1_000_000,
        funding_mode=FundingMode.CASH,
    )
    session_b = TradingSession(
        id=uuid.uuid4(), workspace_id=workspace.id, broker_account_id=broker_account.id,
        started_by_user_id=user.id, mode=SafeMode.PAPER_ONLY, started_at=datetime.now(UTC),
        budget_amount=1_000_000, daily_target_profit=1_000_000, daily_loss_cap=1_000_000,
        funding_mode=FundingMode.CASH,
    )
    db.add_all([session_a, session_b])
    db.flush()

    # A separate underlying per strategy keeps each one's bar/indicator
    # history independent — real usage would typically share one underlying
    # (e.g. NIFTY) across strategies, but nothing about the pipeline
    # requires it, and independent instruments avoid one strategy's bar
    # shape accidentally satisfying (or breaking) another's
    # pattern-matching in this test.
    instruments: dict[str, Instrument] = {}
    for tag in ("orb", "vwap", "ema", "oivol", "sweep"):
        inst = Instrument(
            id=uuid.uuid4(), symbol=f"NIFTY-{tag.upper()}-E2E", exchange="NFO",
            lot_size=25, tick_size=0.05,
        )
        db.add(inst)
        db.flush()
        ce = OptionContract(
            id=uuid.uuid4(), instrument_id=inst.id, expiry_date=EXPIRY, strike=22000,
            option_type=OptionType.CE, symbol=f"NIFTY-{tag.upper()}-E2E-CE",
        )
        pe = OptionContract(
            id=uuid.uuid4(), instrument_id=inst.id, expiry_date=EXPIRY, strike=22000,
            option_type=OptionType.PE, symbol=f"NIFTY-{tag.upper()}-E2E-PE",
        )
        db.add_all([ce, pe])
        db.flush()
        instruments[tag] = inst
        _seed_chain(db, inst, ce, pe, spot=22000.0)

    strategy_configs = {
        "orb": StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="orb-e2e", strategy_type="orb",
        ),
        "vwap": StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="vwap-e2e",
            strategy_type="vwap_pullback",
        ),
        "ema": StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="ema-e2e",
            strategy_type="ema_micro_pullback",
        ),
        "oivol": StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="oivol-e2e",
            strategy_type="oi_volume_confirmed",
        ),
        "sweep": StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="sweep-e2e",
            strategy_type="liquidity_sweep_reversal",
        ),
    }
    db.add_all(strategy_configs.values())
    db.flush()

    # orb: auto mode, session_a. vwap: approval-required, session_a. ema:
    # auto mode, session_b. sweep: auto mode, session_a. oivol:
    # approval-required, session_b — three strategies in one session, two
    # in another, mixed auto/approval-required throughout, satisfying
    # "across multiple sessions".
    session_by_tag = {
        "orb": session_a, "vwap": session_a, "ema": session_b,
        "sweep": session_a, "oivol": session_b,
    }
    mode_by_tag = {
        "orb": ExecutionMode.AUTO, "vwap": ExecutionMode.APPROVAL_REQUIRED,
        "ema": ExecutionMode.AUTO, "sweep": ExecutionMode.AUTO,
        "oivol": ExecutionMode.APPROVAL_REQUIRED,
    }
    strategy_runs = {
        tag: StrategyRun(
            id=uuid.uuid4(), strategy_config_id=strategy_configs[tag].id,
            trading_session_id=session_by_tag[tag].id, execution_mode=mode_by_tag[tag],
            status=StrategyRunStatus.SCANNING, started_at=datetime.now(UTC),
            started_by_user_id=user.id,
        )
        for tag in ("orb", "vwap", "ema", "sweep", "oivol")
    }
    db.add_all(strategy_runs.values())
    db.flush()

    _seed_orb_breakout(db, instruments["orb"])
    _seed_vwap_pullback(db, instruments["vwap"])
    _seed_ema_pullback(db, instruments["ema"])
    _seed_oivol_breakout(db, instruments["oivol"])
    _seed_liquidity_sweep(db, instruments["sweep"])

    strategies = {
        "orb": ORBStrategy(instruments["orb"].id, EXPIRY),
        "vwap": VWAPPullbackStrategy(instruments["vwap"].id, EXPIRY),
        "ema": EMAMicroPullbackStrategy(instruments["ema"].id, EXPIRY),
        "oivol": OIVolumeConfirmedStrategy(instruments["oivol"].id, EXPIRY),
        "sweep": LiquiditySweepReversalStrategy(instruments["sweep"].id, EXPIRY),
    }
    # Each strategy's bar/chain data is already fully seeded above, so one
    # direct run_cycle call per run reaches the same end state real polling
    # would've converged to -- deterministic, no thread/timing dependency.
    for tag, strategy in strategies.items():
        run_cycle(db, strategy, strategy_runs[tag], session_by_tag[tag], strategy_configs[tag])

    orb_intent = (
        db.query(TradeIntent).filter(TradeIntent.strategy_run_id == strategy_runs["orb"].id).one()
    )
    assert orb_intent.status == TradeIntentStatus.DISPATCHED
    orb_position = db.query(Position).filter(Position.trade_intent_id == orb_intent.id).one()
    assert orb_position.status == PositionStatus.OPEN

    vwap_intent = (
        db.query(TradeIntent).filter(TradeIntent.strategy_run_id == strategy_runs["vwap"].id).one()
    )
    assert vwap_intent.status == TradeIntentStatus.PENDING_APPROVAL
    approval = (
        db.query(PendingTradeApproval)
        .filter(PendingTradeApproval.trade_intent_id == vwap_intent.id)
        .one()
    )
    assert approval.status == ApprovalStatus.PENDING

    ema_intent = (
        db.query(TradeIntent).filter(TradeIntent.strategy_run_id == strategy_runs["ema"].id).one()
    )
    assert ema_intent.status == TradeIntentStatus.DISPATCHED

    sweep_intent = (
        db.query(TradeIntent).filter(TradeIntent.strategy_run_id == strategy_runs["sweep"].id).one()
    )
    assert sweep_intent.status == TradeIntentStatus.DISPATCHED

    oivol_intent = (
        db.query(TradeIntent).filter(TradeIntent.strategy_run_id == strategy_runs["oivol"].id).one()
    )
    assert oivol_intent.status == TradeIntentStatus.PENDING_APPROVAL
    oivol_approval = (
        db.query(PendingTradeApproval)
        .filter(PendingTradeApproval.trade_intent_id == oivol_intent.id)
        .one()
    )
    assert oivol_approval.status == ApprovalStatus.PENDING

    assert strategy_runs["orb"].status == StrategyRunStatus.IN_POSITION
    # vwap/oivol never dispatched (still pending approval) -> no Position yet.
    assert strategy_runs["vwap"].status == StrategyRunStatus.SCANNING
    assert strategy_runs["ema"].status == StrategyRunStatus.IN_POSITION
    assert strategy_runs["sweep"].status == StrategyRunStatus.IN_POSITION
    assert strategy_runs["oivol"].status == StrategyRunStatus.SCANNING
