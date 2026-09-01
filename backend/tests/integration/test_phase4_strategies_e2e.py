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
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

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


def _same_session_factory(db: Session):
    """Ops-Hardening Phase 2: run_cycle's `alert_session_factory` parameter
    defaults to the real `session_scope` (bound to the production engine,
    not this test's own isolated one) -- every direct `run_cycle(db, ...)`
    call in this file must override it with this wrapper so a stalled-feed
    alert (if the watchdog happens to fire during a test) lands in this
    test's own rolled-back-at-teardown transaction instead of silently
    committing to the real dev database. Same trap, same fix shape, as this
    project's own documented 2026-08-05 PositionManager incident.
    """

    @contextmanager
    def _factory():
        yield db

    return _factory


@pytest.fixture(autouse=True)
def _fixed_trade_window_clock(monkeypatch):
    """This file drives `run_cycle` directly with real signal-dispatch
    assertions, without regard to wall-clock time. `run_cycle`'s trade-window
    gate (`core.clock.is_within_global_trading_window`, 09:31-15:09 IST) now
    keys off the latest seeded `PriceBar.bucket_start` for every strategy
    here (each fixture below seeds one within that window), not wall-clock
    `now_ist()` -- this monkeypatch only still matters as a safety net for
    the no-bar fallback path (`runner.run_cycle` falls back to `now_ist()`
    when an instrument has no bars at all), which nothing in this file
    currently exercises.
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
    default NIFTY range filter of 20-80 points), anchored to the fixed 9:16
    IST range start (one minute after the real 9:15 session open, skipping
    the open candle -- not `strategy_run.started_at`, which is real
    wall-clock `now()` in this test -- deliberately different, proving the
    anchor is restart-independent), then a bar closing above it."""
    or_start = datetime(2026, 7, 24, 9, 16, tzinfo=IST)
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
    # +16min (9:31 IST), not +15min (9:30) -- must clear both the opening
    # range's own end (or_start + or_minutes) and the global trade window's
    # 09:31 IST start (app.core.clock.is_within_global_trading_window),
    # which now gates on this bar's own bucket_start, not wall-clock now().
    _seed_bar(
        db, instrument, or_start + timedelta(minutes=16),
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
    # IST, not UTC -- 10:00 UTC (15:30 IST) landed the confirmation bar past
    # the global trade window's 15:09 IST close once run_cycle started
    # gating on this bar's own bucket_start instead of wall-clock now().
    base = datetime(2026, 7, 24, 10, 0, tzinfo=IST)
    for i in range(18, 0, -1):
        _seed_bar(
            db, instrument, base - timedelta(minutes=i),
            o=vwap + 20, h=vwap + 25, l=vwap + 15, c=vwap + 20,
        )
    _seed_bar(db, instrument, base, o=22015, h=22020, l=vwap, c=22010)
    _seed_bar(db, instrument, base + timedelta(minutes=1), o=22015, h=22035, l=22012, c=22030)


def _seed_ema_pullback(db, instrument: Instrument) -> None:
    """3 expanding bullish EMA9/EMA20 spreads (20 -> 35 -> 50), 8 filler
    bars with healthy bodies, then a Bone Zone setup bar (low inside the
    EMA9/EMA20 zone, closes above EMA20) and a confirmation bar closing
    above the setup bar's high -- same shape as
    test_ema_micro_pullback_strategy.py's own _seed_bullish_baseline.
    IST 09:41 -- EMA Micro-pullback's own morning trade window is
    09:31-11:00 IST, independent of core.clock.TRADE_WINDOW_START/END (the
    global gate `run_cycle` itself applies, now keyed off this same bar's
    bucket_start rather than wall-clock).
    """
    ema20 = 21950.0
    base = datetime(2026, 7, 24, 9, 41, tzinfo=IST)
    history_start = base - timedelta(minutes=10)
    for i, spread in enumerate([20.0, 35.0, 50.0]):
        ts = history_start + timedelta(minutes=i)
        db.add(IndicatorSnapshot(
            id=uuid.uuid4(), instrument_id=instrument.id, indicator_name="EMA9",
            timeframe=BAR_TIMEFRAME, value=ema20 + spread, ts=ts,
        ))
        db.add(IndicatorSnapshot(
            id=uuid.uuid4(), instrument_id=instrument.id, indicator_name="EMA20",
            timeframe=BAR_TIMEFRAME, value=ema20, ts=ts,
        ))
    for i in range(8):
        _seed_bar(
            db, instrument, history_start + timedelta(minutes=i),
            o=21900, h=21909, l=21899, c=21908,
        )
    _seed_bar(db, instrument, base - timedelta(minutes=1), o=21980, h=21995, l=21960, c=21985)
    _seed_bar(db, instrument, base, o=21990, h=22015, l=21988, c=22010)


def _seed_oivol_breakout(db, instrument: Instrument) -> None:
    """4 filler bars + a 5-bar rolling window flat at [21990, 22030] (width
    40 -- inside OIVolumeConfirmedStrategy's default NIFTY range filter of
    15-60, and healthy-bodied enough that the aggregate 10-bar body ratio
    clears the default 0.40 filter), then a bar closing above it. IST 09:41
    -- OI/Volume Confirmed's own morning trade window is 09:31-11:00 IST,
    independent of core.clock.TRADE_WINDOW_START/END.
    """
    base = datetime(2026, 7, 24, 9, 41, tzinfo=IST)
    start = base - timedelta(minutes=10)
    for i in range(4):
        _seed_bar(
            db, instrument, start + timedelta(minutes=i),
            o=21900, h=21909, l=21899, c=21908,
        )
    window_specs = [
        (22010, 22030, 22000, 22020),
        (22010, 22020, 21990, 22000),
        (22000, 22020, 22000, 22020),
        (22020, 22020, 22000, 22000),
        (22000, 22020, 22000, 22020),
    ]
    for i, (o, h, low, c) in enumerate(window_specs):
        _seed_bar(db, instrument, start + timedelta(minutes=4 + i), o=o, h=h, l=low, c=c)
    _seed_bar(db, instrument, base, o=22030, h=22055, l=22028, c=22050)


def _seed_liquidity_sweep(db, instrument: Instrument) -> PriceBar:
    """10-bar rolling window flat at [21950, 22050] (width 100 -- inside
    LiquiditySweepReversalStrategy's default NIFTY range filter of
    30-120), then a bar that sweeps the window high but closes back inside
    it -> bearish reversal (PE) pending confirmation. Deliberately does NOT
    also seed the confirmation bar -- see
    test_liquidity_sweep_reversal_strategy.py's own _seed_bullish_sweep
    docstring for why (the sweep bar's own run_cycle must process it first,
    while it's genuinely the latest persisted bar). IST 09:45 -- Liquidity
    Sweep/Reversal's own morning trade window is 09:31-11:00 IST.
    """
    base = datetime(2026, 7, 24, 9, 45, tzinfo=IST)
    start = base - timedelta(minutes=12)
    mid, window_high, window_low = 22000.0, 22050.0, 21950.0
    q = (window_high - window_low) / 4
    window_specs = [
        (mid, window_high, mid - q, mid + q),
        (mid, mid + q, window_low, mid - q),
        (mid - q, mid + q, mid - q, mid + q),
        (mid + q, mid + q, mid - q, mid - q),
        (mid - q, mid + q, mid - q, mid + q),
    ]
    for i in range(10):
        o, h, low, c = window_specs[i % len(window_specs)]
        _seed_bar(db, instrument, start + timedelta(minutes=i), o=o, h=h, l=low, c=c)
    sweep_bar = PriceBar(
        id=uuid.uuid4(), instrument_id=instrument.id, timeframe=BAR_TIMEFRAME,
        bucket_start=base - timedelta(minutes=1),
        open=window_high - 20, high=window_high + 20, low=window_high - 25, close=window_high - 10,
        volume=1000,
    )
    db.add(sweep_bar)
    db.flush()
    return sweep_bar


def _seed_liquidity_sweep_confirmation(db, instrument: Instrument, sweep_bar: PriceBar) -> None:
    """Closes below the sweep bar's own low -- the confirmation candle
    that actually fires the trade. Call only after the sweep bar has
    already been through a run_cycle."""
    base = datetime(2026, 7, 24, 9, 45, tzinfo=IST)
    low = float(sweep_bar.low)
    _seed_bar(db, instrument, base, o=low - 5, h=low, l=low - 30, c=low - 25)


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
    sweep_bar = _seed_liquidity_sweep(db, instruments["sweep"])

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
    # Liquidity Sweep/Reversal is the exception: its confirmation-candle
    # gate needs two cycles -- the sweep bar's own cycle just records it as
    # pending, only the *next* bar (seeded now, after that first cycle)
    # actually fires the confirmed trade.
    for tag, strategy in strategies.items():
        if tag == "sweep":
            continue
        run_cycle(
            db,
            strategy,
            strategy_runs[tag],
            session_by_tag[tag],
            strategy_configs[tag],
            alert_session_factory=_same_session_factory(db),
        )

    run_cycle(
        db,
        strategies["sweep"],
        strategy_runs["sweep"],
        session_by_tag["sweep"],
        strategy_configs["sweep"],
        alert_session_factory=_same_session_factory(db),
    )
    _seed_liquidity_sweep_confirmation(db, instruments["sweep"], sweep_bar)
    run_cycle(
        db,
        strategies["sweep"],
        strategy_runs["sweep"],
        session_by_tag["sweep"],
        strategy_configs["sweep"],
        alert_session_factory=_same_session_factory(db),
    )

    orb_intent = (
        db.query(TradeIntent).filter(TradeIntent.strategy_run_id == strategy_runs["orb"].id).one()
    )
    assert orb_intent.status == TradeIntentStatus.DISPATCHED
    orb_position = db.query(Position).filter(Position.trade_intent_id == orb_intent.id).one()
    assert orb_position.status == PositionStatus.OPEN

    vwap_intent = (
        db.query(TradeIntent).filter(TradeIntent.strategy_run_id == strategy_runs["vwap"].id).one()
    )
    # 2026-08-21: both sessions in this test are PAPER_ONLY, and paper trades
    # now always auto-dispatch regardless of execution_mode (approval-
    # required exists to gate real-money risk, which a paper trade carries
    # none of) -- vwap's APPROVAL_REQUIRED setting is now a no-op here. The
    # live case (approval-required genuinely creating a pending approval)
    # is covered directly in test_risk_engine.py instead.
    assert vwap_intent.status == TradeIntentStatus.DISPATCHED
    assert (
        db.query(PendingTradeApproval)
        .filter(PendingTradeApproval.trade_intent_id == vwap_intent.id)
        .one_or_none()
        is None
    )

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
    # 2026-08-21: same reasoning as vwap above -- paper always auto-dispatches.
    assert oivol_intent.status == TradeIntentStatus.DISPATCHED
    assert (
        db.query(PendingTradeApproval)
        .filter(PendingTradeApproval.trade_intent_id == oivol_intent.id)
        .one_or_none()
        is None
    )

    assert strategy_runs["orb"].status == StrategyRunStatus.IN_POSITION
    # 2026-08-21: vwap/oivol now dispatch immediately too (paper auto-
    # dispatch), same as every other strategy here.
    assert strategy_runs["vwap"].status == StrategyRunStatus.IN_POSITION
    assert strategy_runs["ema"].status == StrategyRunStatus.IN_POSITION
    assert strategy_runs["sweep"].status == StrategyRunStatus.IN_POSITION
    assert strategy_runs["oivol"].status == StrategyRunStatus.IN_POSITION
