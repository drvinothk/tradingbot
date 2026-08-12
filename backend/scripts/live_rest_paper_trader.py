"""Temporary REST-poll live paper trader — bridges the gap while TrueData's
WebSocket credentials are pending. Polls a broker-agnostic
`BaseMarketDataProvider` (Angel One today, per `MARKET_DATA_PROVIDER`) for
the underlying's latest completed 1-minute candle, feeds it through the real
production `Strategy`/`submit_signal`/`dispatch_trade_intent` pipeline, and
manages any resulting open position's stop/target/trail every cycle — all of
it real production code, unmodified, the same "reuse the actual pipeline"
discipline `mock_data_tester.py` already established for historical replay.

**Runs as its own standalone process, in parallel with the real backend
service — never inside it.** It creates its own dedicated `TradingSession`/
`StrategyConfig`/`StrategyRun` (mirroring `mock_data_tester.py`'s own
dedicated-row pattern) rather than attaching to an already-running strategy's
session, specifically so its Positions are invisible to that other session's
own `PositionManager` — two processes both calling `evaluate_open_position`/
`close_position` on the same position rows would be a real race. It reuses
the real `workspace_id`/`broker_account_id`/`started_by_user_id` from
whichever real `TradingSession` is currently `ACTIVE`, so this shows up in
the same Reports tab as everything else, not a quarantined test workspace.

**What's real, what's approximated, and why** (read before trusting output):

- The **underlying's own 1-minute OHLCV candle** is real — fetched via
  `BaseMarketDataProvider.get_price_history`, the exact call
  `market_data.ingestion.MarketDataIngestionService._poll_once` already
  makes in production's own WS-health-fallback path. This script calls that
  same method directly (a private method, deliberately — re-implementing its
  candle-persist logic, which already handles `uq_price_bar_bucket`
  idempotency and EMA9/EMA20/ATR14 updates via `IndicatorEngine
  .on_completed_bar`, would only add a second, divergence-prone copy of it)
  on its own wall-clock-aligned schedule instead of that class's own
  free-running interval, to get the `HH:MM:05` alignment this script's own
  spec asked for.
- **Option-contract premiums are never fetched fresh by this script — only
  read from whatever `OptionChainSnapshot` a real, already-running strategy
  is keeping current.** This is a deliberate, load-bearing limitation, not
  an oversight: refreshing an option-chain snapshot needs a real broker's
  `get_option_chain` (Shoonya, in this codebase — Angel One only ever
  supplies underlying candles, never per-strike premiums), and this
  standalone process cannot obtain its own live Shoonya session at all —
  Shoonya's OAuth redirect URL is fixed to the main backend's own running
  `/shoonya/callback` route, so only *that* process's `set_broker()` singleton
  ever gets a real, authenticated `ShoonyaBrokerAdapter`. Calling this
  process's own `get_broker()` instead would silently resolve to the
  *persistent mock* broker and corrupt the real, shared `OptionChainSnapshot`
  row with fake prices — exactly the kind of cross-process mock-contamination
  bug this codebase's own `PositionManager`/`_ensure_symbol_subscribed`
  docstring already warns about in a different guise. So: this script only
  works for an (instrument, expiry) a real strategy elsewhere is *already*
  actively scanning; if none is, `classify_option_chain` reads STALE/DEAD and
  every cycle just logs why and skips new-signal evaluation (existing open
  positions this script itself opened are still managed regardless — only
  *new* entries need a live chain).
- **An option contract's own intra-candle High/Low is therefore
  approximated**, not a genuine broker-reported OHLC candle for that strike
  (no per-strike REST history call exists from this process, per the point
  above) — `_approximate_option_high_low` takes the max/min `ltp` across
  every real `OptionChainSnapshot` sample that landed inside this cycle's
  60-second window (however many a companion strategy's own ~30s scan cycle
  happened to write — typically 1-3). Real, broker-sourced sample points,
  never synthetic — just a coarser sampling than a true tick-by-tick or
  broker-candle High/Low would give. Falls back to a single latest-LTP read
  (High == Low == Close) if no sample landed in the window, degrading
  gracefully to a Close-only check rather than erroring.
- **Entry/exit fill prices are still `MockBrokerAdapter`-simulated** — a
  deterministic symbol-hash seed price, not the real market premium. This is
  not a new quirk this script introduces: `execution_engine.paper.service
  .dispatch_trade_intent`/`close_position` never pass `limit_price` on any
  `OrderRequest` they build, in this entire codebase, for any strategy —
  every paper trade's *recorded* P&L already works this way today (Phase 3's
  own design; see that module's own docstring). What's real is the *trigger*
  logic: stop/target/trail comparisons run against the real prices this
  script fetched, exactly like `PositionManager` does for the live app's own
  strategies — only the broker-side fill/P&L bookkeeping is simulated,
  same as everywhere else in this system.
- **No reconciliation pass, no margin-breach check, no pending-approval
  expiry sweep** — `PositionManager._run_cycle` does all three;
  deliberately dropped here as genuine scope cuts for a *temporary* script,
  not oversights: reconciliation only matters once a real broker executes
  (Phase 6, not yet built), margin-breach only applies to guarded-live/live
  modes (this script only ever creates `paper_only` sessions), and
  `execution_mode="auto"` means no pending approval ever accumulates to
  expire. EOD square-off *is* kept (`_maybe_square_off_at_cutoff`) — an
  unattended script left running past `cutoff_time` shouldn't leave a
  position open forever once it's stopped for the day.

**Modular by construction, not by extra effort**: the only market-data call
this script makes is through `BaseMarketDataProvider` (`get_market_data_
provider()`, selected by the existing `MARKET_DATA_PROVIDER` env var this
codebase already has). A future TrueData WebSocket implementation just needs
to be a new `BaseMarketDataProvider` subclass wired into that same
composition root, exactly like `AngelOneMarketDataProvider` already is —
nothing in this script changes.

Usage (run on the OCI box, real venv, real market hours):
    ./.venv/bin/python scripts/live_rest_paper_trader.py \\
        --symbol NIFTY --strategy ema_micro_pullback

    # explicit expiry, if no real strategy is already scanning the one you want:
    ./.venv/bin/python scripts/live_rest_paper_trader.py \\
        --symbol BANKNIFTY --strategy orb --expiry 2026-08-11

Stops cleanly on Ctrl+C. Re-running resumes management of any position this
script's own `StrategyRun` still has open (state lives in the DB, not this
process — same "durable state, disposable process" discipline `StrategyRunner`
/`PositionManager` restart-resume already established).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time as time_mod
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.core.clock import IST  # noqa: E402
from app.core.db.session import session_scope  # noqa: E402
from app.domain.execution.models import Position, PositionStatus  # noqa: E402
from app.domain.market.models import (
    Instrument,  # noqa: E402
    OptionContract,  # noqa: E402
)
from app.domain.market.models import OptionChainSnapshot as OptionChainSnapshotRow  # noqa: E402
from app.domain.session.models import TradingSession, TradingSessionStatus  # noqa: E402
from app.domain.strategy.models import (  # noqa: E402
    ExecutionMode,
    SignalSide,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
)
from app.modules.broker_adapter.composition import get_execution_broker  # noqa: E402
from app.modules.execution_engine.paper.service import evaluate_open_position  # noqa: E402
from app.modules.market_data.freshness import FreshnessState, classify_option_chain  # noqa: E402
from app.modules.market_data.indicators.engine import IndicatorEngine  # noqa: E402
from app.modules.market_data.ingestion import MarketDataIngestionService  # noqa: E402
from app.modules.market_data.provider_composition import get_market_data_provider  # noqa: E402
from app.modules.strategy_engine.common_rules import (  # noqa: E402
    get_latest_indicator_value,
    get_recent_completed_bars,
)
from app.modules.strategy_engine.interface import Strategy  # noqa: E402
from app.modules.strategy_engine.service import submit_signal  # noqa: E402
from app.modules.strategy_engine.strategies.ema_micro_pullback import (  # noqa: E402
    EMAMicroPullbackStrategy,
)
from app.modules.strategy_engine.strategies.liquidity_sweep_reversal import (  # noqa: E402
    LiquiditySweepReversalStrategy,
)
from app.modules.strategy_engine.strategies.oi_volume_confirmed import (  # noqa: E402
    OIVolumeConfirmedStrategy,
)
from app.modules.strategy_engine.strategies.orb import ORBStrategy  # noqa: E402
from app.modules.strategy_engine.strategies.synthetic import SyntheticStrategy  # noqa: E402
from app.modules.strategy_engine.strategies.vwap_pullback import VWAPPullbackStrategy  # noqa: E402

logger = logging.getLogger("live_rest_paper_trader")

BAR_SECONDS = 60
OPTION_CHAIN_SAMPLE_WINDOW_SECONDS = BAR_SECONDS + 5

STRATEGY_BUILDERS: dict[str, type[Strategy]] = {
    "synthetic": SyntheticStrategy,
    "orb": ORBStrategy,
    "vwap_pullback": VWAPPullbackStrategy,
    "ema_micro_pullback": EMAMicroPullbackStrategy,
    "oi_volume_confirmed": OIVolumeConfirmedStrategy,
    "liquidity_sweep_reversal": LiquiditySweepReversalStrategy,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="NIFTY", choices=["NIFTY", "BANKNIFTY"])
    parser.add_argument(
        "--strategy", choices=sorted(STRATEGY_BUILDERS), default="ema_micro_pullback"
    )
    parser.add_argument("--expiry", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--align-seconds",
        type=int,
        default=5,
        help="Wall-clock second within each minute to poll at (default 5, i.e. HH:MM:05) "
        "— gives the broker's REST API a buffer to have fully finalized the previous "
        "1-minute candle before this script asks for it.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Setup — reuses the real, already-running app's own workspace/instrument
# rows; creates only this script's own dedicated TradingSession/StrategyRun.
# ---------------------------------------------------------------------------


def find_active_real_session(db: Session) -> TradingSession:
    """Borrowed only for its `workspace_id`/`broker_account_id`/
    `started_by_user_id` — this script's own dedicated `TradingSession` reuses
    those so it lands in the same real workspace (and Reports tab) as
    everything else. Never itself written to.
    """
    session = (
        db.query(TradingSession)
        .filter(TradingSession.status == TradingSessionStatus.ACTIVE)
        .order_by(TradingSession.started_at.desc())
        .first()
    )
    if session is None:
        raise SystemExit(
            "No ACTIVE trading session found. Start one from the app first — this script "
            "borrows its workspace/broker-account identity from whichever real session is "
            "currently active, it doesn't create a workspace of its own."
        )
    return session


def resolve_instrument(db: Session, symbol: str) -> Instrument:
    instrument = db.query(Instrument).filter(Instrument.symbol == symbol).one_or_none()
    if instrument is None:
        raise SystemExit(f"No Instrument row for {symbol!r} — sync the instrument master first.")
    return instrument


def resolve_expiry(db: Session, instrument_id: uuid.UUID, explicit: date | None) -> date:
    """Defaults to whichever expiry a real, currently-non-stopped `StrategyRun`
    for this instrument is already scanning — that's what guarantees
    `classify_option_chain` reads LIVE/DEGRADED rather than STALE/DEAD, since
    this script never refreshes the chain itself (see module docstring).
    """
    if explicit is not None:
        return explicit
    run = (
        db.query(StrategyRun)
        .filter(
            StrategyRun.instrument_id == instrument_id,
            StrategyRun.status != StrategyRunStatus.STOPPED,
            StrategyRun.expiry_date.is_not(None),
        )
        .order_by(StrategyRun.started_at.desc())
        .first()
    )
    if run is None or run.expiry_date is None:
        raise SystemExit(
            "No active strategy is currently scanning this instrument, so there's no "
            "already-fresh option-chain snapshot to read from (this script never refreshes "
            "the chain itself — see module docstring). Start a strategy on it from the app "
            "first, or pass --expiry explicitly if you know one is already being kept fresh."
        )
    return run.expiry_date


def create_dedicated_session_and_run(
    db: Session,
    real_session: TradingSession,
    instrument: Instrument,
    expiry: date,
    strategy_type: str,
) -> tuple[TradingSession, StrategyConfig, StrategyRun]:
    defaults = get_settings().risk_defaults
    now = datetime.now(UTC)

    trading_session = TradingSession(
        id=uuid.uuid4(),
        workspace_id=real_session.workspace_id,
        broker_account_id=real_session.broker_account_id,
        started_by_user_id=real_session.started_by_user_id,
        started_at=now,
        budget_amount=defaults.default_budget,
        daily_target_profit=defaults.daily_target_profit,
        daily_loss_cap=defaults.daily_loss_cap,
    )
    db.add(trading_session)
    db.flush()

    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=real_session.workspace_id,
        name=f"live_rest_paper_trader_{strategy_type}_{uuid.uuid4().hex[:8]}",
        strategy_type=strategy_type,
        params={},
    )
    db.add(config)
    db.flush()

    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        started_at=now,
        started_by_user_id=real_session.started_by_user_id,
        instrument_id=instrument.id,
        expiry_date=expiry,
    )
    db.add(run)
    db.flush()
    return trading_session, config, run


# ---------------------------------------------------------------------------
# Per-cycle helpers
# ---------------------------------------------------------------------------


def next_aligned_wake(align_seconds: int) -> datetime:
    now = datetime.now(UTC)
    candidate = now.replace(second=align_seconds, microsecond=0)
    if candidate <= now:
        candidate += timedelta(minutes=1)
    return candidate


def run_cycle_read_only_chain(
    db: Session,
    strategy: Strategy,
    strategy_run: StrategyRun,
    trading_session: TradingSession,
    strategy_config: StrategyConfig,
) -> None:
    """`strategy_engine.runner.run_cycle`'s own evaluate -> submit_signal
    shape, minus its `ensure_fresh_option_chain` refresh — deliberately: that
    call refreshes via `get_broker()`, which in *this* process resolves to
    the persistent mock (no live Shoonya session here — see module
    docstring), and would silently overwrite the real, shared snapshot with
    fake prices. `classify_option_chain` is the read-only half of that same
    module, with no such side effect.
    """
    freshness = classify_option_chain(db, strategy.instrument_id, strategy.expiry_date)
    if freshness in (FreshnessState.STALE, FreshnessState.DEAD):
        logger.warning(
            "option-chain %s for instrument %s expiry %s — skipping new-signal evaluation "
            "this cycle (relies on a companion strategy elsewhere keeping it fresh; "
            "existing open positions are still managed below regardless)",
            freshness.value,
            strategy.instrument_id,
            strategy.expiry_date,
        )
        return

    proposal = strategy.evaluate(db, strategy_run)
    if proposal is None:
        return
    decision = submit_signal(db, strategy_run, trading_session, strategy_config, proposal)
    logger.info(
        "SIGNAL: %s %d lot(s) @ %.2f stop=%.2f target=%.2f | risk: %s%s",
        proposal.side.value.upper(),
        proposal.qty_lots,
        proposal.entry_price,
        proposal.stop_price,
        proposal.target_price,
        decision.decision.value.upper(),
        f" ({', '.join(decision.reasons)})" if decision.reasons else "",
    )


def approximate_option_high_low(
    db: Session,
    instrument_id: uuid.UUID,
    expiry: date,
    contract_symbol: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[float, float, float, float] | None:
    """Returns `(high, low, bid, ask)` approximated from every real
    `OptionChainSnapshot` sample that landed in this cycle's window — see
    module docstring's "what's approximated" section. `bid`/`ask` come from
    whichever sample is latest in the window (the spread-blowout check only
    needs "is liquidity currently poor," not an extreme-specific value).
    `None` if no sample fell in the window at all (no companion strategy
    currently scanning this instrument/expiry) — caller falls back to a
    single latest-LTP read.
    """
    rows = (
        db.query(OptionChainSnapshotRow)
        .filter(
            OptionChainSnapshotRow.instrument_id == instrument_id,
            OptionChainSnapshotRow.expiry_date == expiry,
            OptionChainSnapshotRow.ts >= window_start,
            OptionChainSnapshotRow.ts < window_end,
        )
        .order_by(OptionChainSnapshotRow.ts.asc())
        .all()
    )
    prices: list[float] = []
    latest_bid: float | None = None
    latest_ask: float | None = None
    for row in rows:
        for entry in row.chain_data or []:
            if entry.get("contract_symbol") != contract_symbol:
                continue
            ltp = float(entry.get("ltp") or 0)
            if ltp > 0:
                prices.append(ltp)
            if entry.get("bid") is not None:
                latest_bid = float(entry["bid"])
            if entry.get("ask") is not None:
                latest_ask = float(entry["ask"])
    if not prices:
        return None
    return max(prices), min(prices), latest_bid or min(prices), latest_ask or max(prices)


def manage_open_positions(
    db: Session,
    trading_session: TradingSession,
    strategy_run: StrategyRun,
    instrument_id: uuid.UUID,
    expiry: date,
    window_start: datetime,
    window_end: datetime,
) -> None:
    """Two-pass evaluation per open position — adverse extreme first, then
    favorable — against this cycle's approximated option premium High/Low.
    Extends `evaluate_open_position`'s own "stop checked before target"
    ordering to *intra-candle* ordering: this script only polls once per
    completed 1-minute candle (unlike `PositionManager`'s continuous ~3s
    ticks), so checking only the close would miss a spike-and-recover that
    happened entirely between two of this script's own polls.
    """
    broker = get_execution_broker(trading_session)
    positions = (
        db.query(Position)
        .filter(
            Position.trading_session_id == trading_session.id,
            Position.status == PositionStatus.OPEN,
        )
        .all()
    )
    for position in positions:
        option_contract = db.get(OptionContract, position.option_contract_id)
        if option_contract is None:
            continue
        sample = approximate_option_high_low(
            db, instrument_id, expiry, option_contract.symbol, window_start, window_end
        )
        if sample is None:
            logger.warning(
                "no option-chain sample for %s in this window — skipping stop/target/trail "
                "check this cycle rather than trading on a stale price",
                option_contract.symbol,
            )
            continue
        high, low, bid, ask = sample

        favorable = SignalSide(position.side) == SignalSide.BUY
        adverse_price, favorable_price = (low, high) if favorable else (high, low)

        outcome = evaluate_open_position(
            db, trading_session, position, adverse_price, broker=broker, bid=bid, ask=ask
        )
        if outcome is not None:
            logger.info(
                "EXIT (adverse leg): %s realized_pnl=%.2f",
                option_contract.symbol,
                outcome.realized_pnl,
            )
            continue

        outcome = evaluate_open_position(
            db, trading_session, position, favorable_price, broker=broker, bid=bid, ask=ask
        )
        if outcome is not None:
            logger.info(
                "EXIT (favorable leg): %s realized_pnl=%.2f",
                option_contract.symbol,
                outcome.realized_pnl,
            )


def maybe_square_off_at_cutoff(db: Session, trading_session: TradingSession) -> None:
    from app.core.clock import now_ist
    from app.modules.scheduler.eod_square_off import run_eod_square_off

    if now_ist().time() >= trading_session.cutoff_time:
        broker = get_execution_broker(trading_session)
        run_eod_square_off(db, broker, trading_session)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()

    with session_scope() as db:
        real_session = find_active_real_session(db)
        instrument = resolve_instrument(db, args.symbol)
        expiry = resolve_expiry(db, instrument.id, args.expiry)
        trading_session, strategy_config, strategy_run = create_dedicated_session_and_run(
            db, real_session, instrument, expiry, args.strategy
        )
        trading_session_id = trading_session.id
        strategy_run_id = strategy_run.id
        strategy_config_id = strategy_config.id
        instrument_id = instrument.id
        symbol = instrument.symbol

    strategy = STRATEGY_BUILDERS[args.strategy](instrument_id, expiry)
    provider = get_market_data_provider()
    provider.connect()
    ingestion = MarketDataIngestionService(
        provider, session_factory=session_scope, indicator_engine=IndicatorEngine()
    )

    print(f"\n{'=' * 90}")
    print(f"live_rest_paper_trader: {args.strategy} on {symbol}, expiry {expiry}")
    print(f"trading_session_id={trading_session_id} strategy_run_id={strategy_run_id}")
    print(f"Polling every 60s at HH:MM:{args.align_seconds:02d}. Ctrl+C to stop.")
    print(f"{'=' * 90}\n")

    try:
        while True:
            wake_at = next_aligned_wake(args.align_seconds)
            time_mod.sleep(max((wake_at - datetime.now(UTC)).total_seconds(), 0.0))

            try:
                ingestion._poll_once(symbol, instrument_id)  # noqa: SLF001 — see module docstring
            except Exception:  # noqa: BLE001 - one bad poll must not kill the loop
                logger.exception("underlying candle poll failed; will retry next cycle")
                continue

            window_end = datetime.now(UTC)
            window_start = window_end - timedelta(seconds=OPTION_CHAIN_SAMPLE_WINDOW_SECONDS)

            with session_scope() as db:
                run = db.get(StrategyRun, strategy_run_id)
                session_row = db.get(TradingSession, trading_session_id)
                config_row = db.get(StrategyConfig, strategy_config_id)
                if run is None or session_row is None or config_row is None:
                    logger.error("dedicated session/run/config row missing — stopping")
                    return
                if session_row.status != TradingSessionStatus.ACTIVE:
                    logger.warning("trading session no longer ACTIVE — stopping")
                    return

                latest_bars = get_recent_completed_bars(db, instrument_id, limit=1)
                if latest_bars:
                    bar = latest_bars[0]
                    ema9 = get_latest_indicator_value(db, instrument_id, "EMA9")
                    ema20 = get_latest_indicator_value(db, instrument_id, "EMA20")
                    logger.info(
                        "[%s] O=%.2f H=%.2f L=%.2f C=%.2f | EMA9=%s EMA20=%s",
                        bar.bucket_start.astimezone(IST).strftime("%H:%M"),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        f"{ema9:.2f}" if ema9 is not None else "n/a",
                        f"{ema20:.2f}" if ema20 is not None else "n/a",
                    )

                manage_open_positions(
                    db, session_row, run, instrument_id, expiry, window_start, window_end
                )
                run_cycle_read_only_chain(db, strategy, run, session_row, config_row)
                maybe_square_off_at_cutoff(db, session_row)

                if run.status != StrategyRunStatus.STOPPED:
                    has_position = (
                        db.query(Position)
                        .filter(
                            Position.trading_session_id == trading_session_id,
                            Position.status == PositionStatus.OPEN,
                        )
                        .first()
                        is not None
                    )
                    new_status = (
                        StrategyRunStatus.IN_POSITION
                        if has_position
                        else StrategyRunStatus.SCANNING
                    )
                    if run.status != new_status:
                        run.status = new_status
                        db.add(run)
    except KeyboardInterrupt:
        print("\nStopped. Any open position stays open in the DB — re-run to resume managing it.")


if __name__ == "__main__":
    main()
