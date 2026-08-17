"""Replays real historical market data through the actual production
strategy pipeline (`strategy_engine.runner.run_cycle`, the real `Strategy`
subclasses, and Risk Service's `evaluate_trade_intent`) and reports
simulated trade performance — not a reimplementation of any strategy's
entry logic, per the same "reuse the real pipeline" principle a prior
backtest harness on this branch used (that harness, plus its
`query_backtest_trades.py` exit-reconstruction companion, was deleted
2026-08-15 for an unrelated reason — see CLAUDE.md's Phase 5 section and
project memory `project_backtest_infra_and_atr_risk_engine.md`; this is a
fresh implementation, not a recovery of that code).

Usage:
    python scripts/run_backtest.py --strategy orb --underlying NIFTY \\
        --from 2026-08-13 --to 2026-08-17

Requires `backend/data/historical/` populated by
`scripts/fetch_truedata_historical.py` first (see that script's own
docstring) — this script fails loudly, not silently, if the expected CSVs
aren't there.

Every `StrategyRun` here uses `execution_mode=APPROVAL_REQUIRED` (never
`AUTO`), so a risk-approved signal reaches `TradeIntentStatus
.PENDING_APPROVAL` and stops there — `risk_engine.service
.evaluate_trade_intent` only calls `execution_engine.paper.service
.dispatch_trade_intent` on `DISPATCHED`, which only happens in `AUTO` mode
(confirmed by reading that function directly, not assumed) — so no real
`Position`/`Order`/`StopPlan`/`TrailPlan` row is ever created and
`PositionManager` never needs to run. Instead, this script's own
`_reconstruct_exit` walks the saved option-contract bars forward from each
approved `TradeIntent`'s entry point, applying the *same* stop/target/trail
math `execution_engine.paper.service.evaluate_open_position` uses (steps 1,
2, 5 of that function — see its own docstring), to compute a realized exit.

Known, deliberate approximations/limitations (also called out at the end of
the printed report — do not treat this backtest's numbers as more precise
than they are):

- **No structure-break or spread-blowout exits are modeled** (steps 3 and 4
  of `evaluate_open_position`). A real run would exit some trades earlier
  (or at a different price) than this reconstruction shows — PnL here reads
  more optimistic than a real run's would.
- **Close-only pricing, not intrabar high/low.** Every stop/target/trail
  check below compares each subsequent option bar's `close` against the
  relevant level — a real position could be stopped/targeted intrabar on a
  wick this never sees. Both this and the point above carry forward the
  exact same caveats the deleted prior harness's own docstring already
  flagged, per the task's own instruction to preserve them rather than
  silently claim they're solved.
- **VWAP is computed from bar close+volume, one sample per completed bar**,
  not true tick-level cumulative VWAP. `IndicatorEngine.on_completed_bar`
  (the method this script uses for EMA9/EMA20, since historical rows are
  already-completed bars, not raw ticks) deliberately skips VWAP entirely —
  see that method's own docstring ("a discrete completed candle can't
  correctly feed a tick/volume-cumulative concept one sample at a time").
  This script feeds `market_data.indicators.vwap.VWAPCalculator` directly,
  once per bar, as the best available approximation; VWAP Pullback is the
  only strategy this affects.
- **Real finding, not a backtest-script bug**: TrueData's underlying index
  bars (NIFTY/BANKNIFTY) always carry `volume=0` (confirmed in the fetched
  CSVs — real, not a fetch defect; an index has no traded volume of its
  own). `VWAPCalculator.value` is `None` for as long as cumulative volume
  stays `0`, which it always will off underlying-index bars alone. VWAP
  Pullback will very likely never fire against this data for exactly this
  reason — and since production's own live feeds report the same `volume=0`
  for index ticks (`PriceCandle`'s own docstring says as much), this is
  very likely a pre-existing gap in live VWAP Pullback too, not something
  introduced by this backtest. Flagged here, not fixed — out of scope for
  this task.
- **Bid/ask are synthetic.** TrueData's historical REST endpoint returns
  OHLCV+OI only, never a real bid/ask — `HistoricalBrokerAdapter
  .get_option_chain` fabricates a +/-0.25% half-spread around each bar's
  close so `strike_ranking.rank_strikes`'s spread-pct filter/score has
  something to operate on. A real spread would change which strike gets
  picked some of the time.
- `Instrument.lot_size`/`tick_size` (`UNDERLYING_META` below) reuse the same
  illustrative test values `domain.market.mock_universe._UNDERLYINGS`
  already uses (NIFTY 25, BANKNIFTY 15) — not independently re-verified
  against a current NSE circular (see that module's own docstring for why:
  "real NSE F&O ... quantities are periodically revised, never a fact to
  hardcode"). `RiskLimitConfig`/`TradingSession`
  budget/loss-cap/profit-target values are set deliberately generous so the
  backtest exercises real risk checks (tick-size alignment, price-drift,
  same-strike locking, margin) without being gated by arbitrary limits.
- **No cross-trade P&L feedback.** Since no `Position` ever closes (see
  above), `TradingSession.cumulative_realized_pnl`/`consecutive_losses`
  never update and `record_trade_outcome_effects` never runs — every
  signal is risk-evaluated independently of this backtest's own prior
  simulated outcomes, same limitation the deleted prior harness had for the
  identical structural reason.
- **Indicator warm-up**: this script primes `IndicatorEngine`/VWAP off
  every available underlying bar *before* `--from` (persisted, but with
  `strategy.evaluate()` never called) so EMA9/EMA20 aren't cold on the
  first replayed day — but a strategy's own in-memory per-run state
  (`ORBStrategy._fired_directions`, `EMAMicroPullbackStrategy
  .trades_fired_count`, etc.) still starts fresh at `--from`, same as a
  real `StrategyRunner` restarting fresh that morning would.
- **The option-chain freshness gate is wall-clock-based, not
  simulation-clock-based** (`market_data.freshness.ensure_fresh_option_chain`
  compares a snapshot's age against real `datetime.now(UTC)`). A fast
  replay would otherwise refresh the very first snapshot, then keep reading
  it as "fresh" for the rest of the run since real wall-clock barely moves
  between cycles. This script works around that explicitly by deleting the
  `OptionChainSnapshot` row for this instrument+expiry before every cycle,
  forcing `ensure_fresh_option_chain` to always see "no snapshot" (DEAD) and
  fetch a new one from `HistoricalBrokerAdapter` at the current simulated
  time — not a threshold hack, an explicit, deterministic reset.
"""

from __future__ import annotations

import argparse
import csv
import sys
import uuid
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.api.v1.strategies import _build_strategy  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.core.clock import IST, to_ist  # noqa: E402
from app.core.db.base import Base  # noqa: E402
from app.domain import (  # noqa: E402,F401 - registers every domain model on Base
    audit,
    broker,
    execution,
    identity,
    market,
    ops,
    risk,
    session,
    strategy,
)
from app.domain.identity.models import (  # noqa: E402
    BrokerAccount,
    BrokerAccountStatus,
    BrokerType,
    User,
    Workspace,
)
from app.domain.market.models import IndicatorSnapshot as IndicatorSnapshotRow  # noqa: E402
from app.domain.market.models import Instrument, OptionContract  # noqa: E402
from app.domain.market.models import OptionChainSnapshot as OptionChainSnapshotRow  # noqa: E402
from app.domain.market.models import OptionType as DomainOptionType  # noqa: E402
from app.domain.market.models import PriceBar as PriceBarRow  # noqa: E402
from app.domain.market.models import QuoteTick as QuoteTickRow  # noqa: E402
from app.domain.risk.models import RiskDecision, RiskLimitConfig  # noqa: E402
from app.domain.session.models import (  # noqa: E402
    FundingMode,
    SafeMode,
    TradingSession,
    TradingSessionStatus,
)
from app.domain.strategy.models import (  # noqa: E402
    ExecutionMode,
    Signal,
    SignalSide,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.broker_adapter.base.broker_port import (  # noqa: E402
    BrokerPort,
    DepthCallback,
    TickCallback,
)
from app.modules.broker_adapter.base.contracts import (  # noqa: E402
    AuthResult,
    DepthSnapshot,
    InstrumentInfo,
    MarginInfo,
    OptionChainEntry,
    OptionChainSnapshot,
    OrderRequest,
    OrderResult,
    PriceCandle,
    Tick,
)
from app.modules.broker_adapter.base.contracts import (
    OptionType as ContractOptionType,
)
from app.modules.broker_adapter.base.contracts import (
    Position as BrokerPosition,
)
from app.modules.broker_adapter.composition import reset_for_tests, set_broker  # noqa: E402
from app.modules.execution_engine.paper.service import (  # noqa: E402
    TRAIL_ACTIVATION_FRACTION,
    TRAIL_LOCK_FRACTION,
)
from app.modules.market_data.indicators.engine import IndicatorEngine  # noqa: E402
from app.modules.market_data.indicators.vwap import VWAPCalculator  # noqa: E402
from app.modules.strategy_engine.runner import run_cycle  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "historical"

# Same "test value, not a verified current NSE-published figure" convention
# `mock_universe._UNDERLYINGS` already uses — kept identical to that module's
# values for internal consistency, not independently re-derived here.
UNDERLYING_META: dict[str, tuple[int, float]] = {  # (lot_size, tick_size)
    "NIFTY": (25, 0.05),
    "BANKNIFTY": (15, 0.05),
}

# Matches TradingSession.cutoff_time's own real default (see
# app/domain/session/models.py) and strategy_engine.common_rules
# .BAR_TIMEFRAME's 60s convention — the only timeframe anything in this
# codebase persists.
EOD_CUTOFF = time(15, 9)
BAR_TIMEFRAME = "60s"

STRATEGY_TYPES = (
    "orb",
    "vwap_pullback",
    "ema_micro_pullback",
    "oi_volume_confirmed",
    "liquidity_sweep_reversal",
)


class Bar(NamedTuple):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------


def _parse_bar_row(row: dict[str, str]) -> Bar:
    ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    return Bar(
        ts=ts,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(float(row["volume"])),
        oi=int(float(row["oi"])),
    )


def _load_csv_bars(path: Path) -> list[Bar]:
    with path.open(newline="") as f:
        bars = [_parse_bar_row(row) for row in csv.DictReader(f)]
    bars.sort(key=lambda b: b.ts)
    return bars


def _load_underlying_bars(data_dir: Path, underlying: str) -> list[Bar]:
    path = data_dir / "underlyings" / f"{underlying}_1min.csv"
    if not path.is_file():
        raise SystemExit(
            f"Missing underlying data file: {path}\n"
            "Run scripts/fetch_truedata_historical.py first (see its own docstring)."
        )
    bars = _load_csv_bars(path)
    if not bars:
        raise SystemExit(f"{path} exists but has no rows.")
    return bars


def _discover_expiry_dir(
    data_dir: Path, underlying: str, from_date: date, override: date | None
) -> tuple[date, Path]:
    base = data_dir / "options" / underlying
    if not base.is_dir():
        raise SystemExit(
            f"Missing option data directory: {base}\n"
            "Run scripts/fetch_truedata_historical.py first (see its own docstring)."
        )
    if override is not None:
        expiry_dir = base / override.isoformat()
        if not expiry_dir.is_dir():
            raise SystemExit(f"No option data for --expiry {override.isoformat()} under {base}")
        return override, expiry_dir

    candidates: list[date] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        try:
            candidates.append(date.fromisoformat(child.name))
        except ValueError:
            continue
    if not candidates:
        raise SystemExit(f"No expiry subdirectories found under {base}")
    chosen = min(candidates, key=lambda d: abs((d - from_date).days))
    return chosen, base / chosen.isoformat()


def _parse_option_symbol(underlying: str, stem: str) -> tuple[float, DomainOptionType]:
    """`{underlying}{expiry:%y%m%d}{strike}{CE|PE}` — TrueData's real
    option-symbol convention, confirmed live and used by
    `fetch_truedata_historical.py._option_symbol` (a different convention
    from Shoonya's own P/C-suffix format — see that script's own docstring;
    don't conflate the two).
    """
    if not stem.startswith(underlying):
        raise ValueError(f"option filename {stem!r} doesn't start with underlying {underlying!r}")
    rest = stem[len(underlying) :]
    option_type_str = rest[-2:]
    if option_type_str not in ("CE", "PE"):
        raise ValueError(f"option filename {stem!r} has an unexpected CE/PE suffix")
    strike_str = rest[6:-2]  # 6-digit YYMMDD expiry prefix, then the strike
    return float(strike_str), DomainOptionType(option_type_str)


def _load_option_bars(
    underlying: str, expiry_dir: Path, from_date: date, to_date: date
) -> tuple[list[tuple[str, float, ContractOptionType]], dict[str, list[Bar]]]:
    contracts: list[tuple[str, float, ContractOptionType]] = []
    option_bars: dict[str, list[Bar]] = {}
    for path in sorted(expiry_dir.glob("*.csv")):
        symbol = path.stem
        strike, option_type = _parse_option_symbol(underlying, symbol)
        bars = [b for b in _load_csv_bars(path) if from_date <= b.ts.date() <= to_date]
        if not bars:
            continue
        option_bars[symbol] = bars
        contracts.append((symbol, strike, ContractOptionType(option_type.value)))
    if not contracts:
        raise SystemExit(
            f"No option contracts under {expiry_dir} have any bars in "
            f"[{from_date.isoformat()}, {to_date.isoformat()}]"
        )
    return contracts, option_bars


# ---------------------------------------------------------------------------
# Historical BrokerPort
# ---------------------------------------------------------------------------


class HistoricalBrokerAdapter(BrokerPort):
    """Serves `get_option_chain` from saved historical option-contract bars,
    as of whatever `set_simulated_time` was last called with — installed via
    `broker_adapter.composition.set_broker` so `strategy_engine.runner
    .run_cycle`'s call to `get_broker()` (for the freshness-gated chain
    refresh) resolves to this instance with zero changes to `run_cycle`
    itself.

    Every other `BrokerPort` method raises `NotImplementedError` — see
    `_not_implemented`'s own message for why that's provably safe here, not
    just untested.
    """

    def __init__(
        self,
        contracts: list[tuple[str, float, ContractOptionType]],
        option_bars: dict[str, list[Bar]],
    ) -> None:
        self._contracts = contracts
        self._option_bars = option_bars
        self._option_ts_index: dict[str, list[datetime]] = {
            symbol: [b.ts for b in bars] for symbol, bars in option_bars.items()
        }
        self._simulated_now: datetime | None = None

    def set_simulated_time(self, ts: datetime) -> None:
        self._simulated_now = ts

    def get_option_chain(self, underlying: str, expiry: date) -> OptionChainSnapshot:
        if self._simulated_now is None:
            raise RuntimeError("set_simulated_time() must be called before get_option_chain()")
        entries: list[OptionChainEntry] = []
        for symbol, strike, option_type in self._contracts:
            ts_list = self._option_ts_index.get(symbol)
            if not ts_list:
                continue
            idx = bisect_right(ts_list, self._simulated_now) - 1
            if idx < 0:
                continue  # this contract has no data yet as of this simulated moment
            bar = self._option_bars[symbol][idx]
            half_spread = bar.close * 0.0025
            entries.append(
                OptionChainEntry(
                    contract_symbol=symbol,
                    strike=strike,
                    option_type=option_type,
                    ltp=bar.close,
                    bid=bar.close - half_spread,
                    ask=bar.close + half_spread,
                    volume=bar.volume,
                    oi=bar.oi,
                )
            )
        # ts is real wall-clock `now`, deliberately not the simulated time —
        # see module docstring's "option-chain freshness gate" section for
        # why: this snapshot is always freshly deleted-then-refetched by the
        # caller immediately before use, so its own ts only needs to satisfy
        # ensure_fresh_option_chain's real-wall-clock LIVE check, not carry
        # simulated time anywhere meaningful.
        return OptionChainSnapshot(
            underlying=underlying, expiry=expiry, ts=datetime.now(UTC), entries=tuple(entries)
        )

    def get_margin(self) -> MarginInfo:
        # Generous fixed value, explicitly flagged as an approximation, same
        # "flag it, don't pretend it's modeled" style as the deleted ATR
        # risk engine's flat 0.5 ATM-delta placeholder (see project memory
        # entry project_backtest_infra_and_atr_risk_engine.md). In practice
        # this is never on the call path that matters for a paper_only
        # session: evaluate_trade_intent's margin check resolves
        # get_execution_broker(trading_session), which returns the
        # *persistent MockBrokerAdapter* for paper_only, not this adapter
        # (which is only ever installed as get_broker()'s market-data-side
        # singleton) — implemented anyway to satisfy BrokerPort's contract
        # and in case a future caller resolves it differently.
        return MarginInfo(
            available_margin=10_000_000.0,
            used_margin=0.0,
            total_margin=10_000_000.0,
            ts=datetime.now(UTC),
        )

    def _not_implemented(self, name: str) -> NoReturn:
        raise NotImplementedError(
            f"HistoricalBrokerAdapter.{name}() is never called in this backtest: every "
            "StrategyRun uses execution_mode=APPROVAL_REQUIRED, so "
            "risk_engine.service.evaluate_trade_intent never marks a TradeIntent "
            "DISPATCHED (only AUTO execution mode does that), so "
            "strategy_engine.service.submit_signal never calls "
            "execution_engine.paper.service.dispatch_trade_intent, which is the only "
            "caller of place_order/get_positions/subscribe_quotes/etc. If this fires, "
            "something upstream changed and this adapter's contract needs revisiting."
        )

    def authenticate(self) -> AuthResult:
        self._not_implemented("authenticate")

    def get_instrument_master(self, exchange: str) -> list[InstrumentInfo]:
        self._not_implemented("get_instrument_master")

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        self._not_implemented("get_price_history")

    def get_quote(self, contract_symbol: str) -> Tick:
        self._not_implemented("get_quote")

    def get_depth(self, contract_symbol: str) -> DepthSnapshot:
        self._not_implemented("get_depth")

    def subscribe_quotes(
        self,
        contract_symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        self._not_implemented("subscribe_quotes")

    def unsubscribe_quotes(self, contract_symbols: list[str]) -> None:
        self._not_implemented("unsubscribe_quotes")

    def place_order(self, request: OrderRequest) -> OrderResult:
        self._not_implemented("place_order")

    def modify_order(self, broker_order_id: str, **changes: object) -> OrderResult:
        self._not_implemented("modify_order")

    def cancel_order(self, broker_order_id: str) -> OrderResult:
        self._not_implemented("cancel_order")

    def get_order_status(self, broker_order_id: str) -> OrderResult:
        self._not_implemented("get_order_status")

    def get_positions(self) -> list[BrokerPosition]:
        self._not_implemented("get_positions")


# ---------------------------------------------------------------------------
# Isolated backtest database
# ---------------------------------------------------------------------------


def _backtest_db_name() -> str:
    return f"{get_settings().db.name}_backtest"


def _backtest_database_url() -> str:
    base_url = get_settings().db.sqlalchemy_url.rsplit("/", 1)[0]
    return f"{base_url}/{_backtest_db_name()}"


def _ensure_backtest_database_exists() -> None:
    """Mirrors tests/conftest.py's `_ensure_test_database_exists` exactly —
    a dedicated `<DB_NAME>_backtest` database, never `DB_NAME`/`DB_NAME_test`
    directly, created on demand via a maintenance connection to `postgres`.
    """
    maintenance_url = get_settings().db.sqlalchemy_url.rsplit("/", 1)[0] + "/postgres"
    maintenance_engine = create_engine(maintenance_url, future=True, isolation_level="AUTOCOMMIT")
    try:
        with maintenance_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": _backtest_db_name()},
            ).first()
            if exists is None:
                conn.execute(text(f'CREATE DATABASE "{_backtest_db_name()}"'))
    finally:
        maintenance_engine.dispose()


@dataclass
class SeedContext:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    trading_session_id: uuid.UUID
    strategy_config_id: uuid.UUID
    strategy_run_id: uuid.UUID
    instrument_id: uuid.UUID
    lot_size: int
    tick_size: float


def _seed_backtest_entities(
    db_scope: DbScope,
    *,
    underlying: str,
    strategy_type: str,
    expiry_date: date,
    contracts: list[tuple[str, float, ContractOptionType]],
    from_date: date,
) -> SeedContext:
    """Minimal real rows for one full replay — see module docstring's
    "Seed the minimal real rows needed" section in the task for the exact
    list this follows. Generous but non-zero risk limits: the point is to
    exercise the real risk-check pipeline (tick alignment, price drift,
    same-strike locking, margin), not bypass it.
    """
    lot_size, tick_size = UNDERLYING_META[underlying]

    with db_scope() as db:
        workspace = Workspace(id=uuid.uuid4(), name=f"backtest-{uuid.uuid4().hex[:8]}")
        db.add(workspace)
        db.flush()

        user = User(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            email=f"backtest-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="unused-backtest-user",  # never authenticated against
            display_name="Backtest Runner",
            is_active=True,
        )
        db.add(user)
        db.flush()

        broker_account = BrokerAccount(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            broker_type=BrokerType.SHOONYA,
            label="Backtest",
            credentials_ref="backtest",
            status=BrokerAccountStatus.ACTIVE,
        )
        db.add(broker_account)
        db.flush()

        risk_config = RiskLimitConfig(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            version=1,
            is_active=True,
            max_concurrent_positions=10,
            max_trades_per_day=100,
            consecutive_loss_pause_threshold=100,
            daily_loss_cap=Decimal("10000000"),
            daily_target_profit=Decimal("10000000"),
            per_trade_lot_cap=10,
        )
        db.add(risk_config)
        db.flush()

        instrument = Instrument(
            id=uuid.uuid4(),
            symbol=underlying,
            exchange="NFO",
            lot_size=lot_size,
            tick_size=tick_size,
            freeze_qty=None,
            is_active=True,
        )
        db.add(instrument)
        db.flush()

        for symbol, strike, option_type in contracts:
            db.add(
                OptionContract(
                    id=uuid.uuid4(),
                    instrument_id=instrument.id,
                    expiry_date=expiry_date,
                    strike=Decimal(str(strike)),
                    option_type=DomainOptionType(option_type.value),
                    symbol=symbol,
                    broker_token="",
                    is_active=True,
                )
            )
        db.flush()

        session_start = datetime.combine(from_date, time(9, 0), tzinfo=IST)
        trading_session = TradingSession(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            broker_account_id=broker_account.id,
            started_by_user_id=user.id,
            mode=SafeMode.PAPER_ONLY,
            status=TradingSessionStatus.ACTIVE,
            started_at=session_start,
            cutoff_time=EOD_CUTOFF,
            budget_amount=Decimal("10000000"),
            daily_target_profit=Decimal("10000000"),
            daily_loss_cap=Decimal("10000000"),
            funding_mode=FundingMode.CASH,
        )
        db.add(trading_session)
        db.flush()

        strategy_config = StrategyConfig(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            name=f"backtest-{strategy_type}-{uuid.uuid4().hex[:8]}",
            strategy_type=strategy_type,
            params={},
            is_enabled=True,
            underlying_symbol=underlying,
        )
        db.add(strategy_config)
        db.flush()

        strategy_run = StrategyRun(
            id=uuid.uuid4(),
            strategy_config_id=strategy_config.id,
            trading_session_id=trading_session.id,
            execution_mode=ExecutionMode.APPROVAL_REQUIRED,
            status=StrategyRunStatus.SCANNING,
            started_at=session_start,
            started_by_user_id=user.id,
            instrument_id=instrument.id,
            expiry_date=expiry_date,
            interval_seconds=None,
        )
        db.add(strategy_run)
        db.flush()

        return SeedContext(
            workspace_id=workspace.id,
            user_id=user.id,
            trading_session_id=trading_session.id,
            strategy_config_id=strategy_config.id,
            strategy_run_id=strategy_run.id,
            instrument_id=instrument.id,
            lot_size=lot_size,
            tick_size=tick_size,
        )


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

# A short-lived transaction, opened and committed once per bar/cycle — the
# same shape `StrategyRunner._loop`'s own `session_factory` parameter has
# (see runner.py's own docstring), reused here so this script's replay loop
# matches production's real session-per-cycle discipline instead of holding
# one long-lived transaction across the whole backtest.
DbScope = Callable[[], AbstractContextManager[Session]]


def _persist_underlying_bar(
    db: Session,
    instrument_id: uuid.UUID,
    bar: Bar,
    indicator_engine: IndicatorEngine,
    vwap_calc: VWAPCalculator,
) -> None:
    """Mirrors `market_data.ingestion.MarketDataIngestionService
    ._persist_candle`'s insert pattern exactly (QuoteTick + IndicatorSnapshot
    per updated indicator + PriceBar), with one addition: a direct
    `VWAPCalculator.update` call, since `IndicatorEngine.on_completed_bar`
    (the real method for already-completed historical bars) deliberately
    skips VWAP — see module docstring.
    """
    candle = PriceCandle(
        bucket_start=bar.ts, open=bar.open, high=bar.high, low=bar.low, close=bar.close,
        volume=bar.volume,
    )
    db.add(
        QuoteTickRow(
            id=uuid.uuid4(),
            instrument_id=instrument_id,
            option_contract_id=None,
            ltp=bar.close,
            bid=bar.close,
            ask=bar.close,
            volume=bar.volume,
            oi=None,
            ts=bar.ts + timedelta(seconds=60),
        )
    )

    updated = dict(indicator_engine.on_completed_bar(instrument_id, candle))
    vwap_value = vwap_calc.update(bar.close, bar.volume)
    if vwap_value is not None:
        updated["VWAP"] = vwap_value

    for indicator_name, value in updated.items():
        db.add(
            IndicatorSnapshotRow(
                id=uuid.uuid4(),
                instrument_id=instrument_id,
                indicator_name=indicator_name,
                timeframe=BAR_TIMEFRAME,
                value=value,
                ts=bar.ts,
            )
        )

    db.add(
        PriceBarRow(
            id=uuid.uuid4(),
            instrument_id=instrument_id,
            timeframe=BAR_TIMEFRAME,
            bucket_start=bar.ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
    )


def _correct_timestamps(db: Session, trade_intent_id: uuid.UUID, simulated_time: datetime) -> None:
    """Post-hoc timestamp correction, same approach project memory records
    for the deleted prior harness: `submit_signal` stamps `Signal
    .generated_at`/`TradeIntent.created_at`/`RiskDecision.created_at` with
    real wall-clock `datetime.now(UTC)` — this corrects them, via the
    RiskDecision -> TradeIntent -> Signal FK chain, to the simulated bar
    time this cycle actually represents. No `freezegun`-style clock mocking
    is used (confirmed: zero hits for that dependency in this repo).
    """
    trade_intent = db.get(TradeIntent, trade_intent_id)
    if trade_intent is None:
        return
    trade_intent.created_at = simulated_time
    if trade_intent.dispatched_at is not None:
        trade_intent.dispatched_at = simulated_time
    db.add(trade_intent)

    signal = db.get(Signal, trade_intent.signal_id)
    if signal is not None:
        signal.generated_at = simulated_time
        db.add(signal)

    for decision in db.query(RiskDecision).filter(RiskDecision.trade_intent_id == trade_intent.id):
        decision.created_at = simulated_time
        db.add(decision)

    db.flush()


@dataclass
class ReconstructedTrade:
    symbol: str
    side: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime | None
    exit_price: float | None
    exit_reason: str
    qty_lots: int
    lot_size: int
    pnl: float | None = field(default=None)


def _reconstruct_exit(
    trade_intent: TradeIntent, symbol: str, option_bars: list[Bar], lot_size: int
) -> ReconstructedTrade:
    """Walks `option_bars` forward from `trade_intent`'s (already
    timestamp-corrected) entry point, applying steps 1 (stop), 2 (target),
    and 5 (trail) of `execution_engine.paper.service.evaluate_open_position`
    — see that function's own docstring for the full priority order this
    intentionally omits steps 3/4 from (structure-break, spread-blowout;
    see module docstring's "Known limitations" section).

    An unconditional EOD square-off at `EOD_CUTOFF` is checked first each
    bar (mirroring `scheduler.eod_square_off`'s own "unconditional, not
    routed through evaluate_open_position" behavior), not folded into the
    stop/target/trail priority order below it.
    """
    entry_time = trade_intent.created_at
    entry_price = Decimal(str(trade_intent.entry_price))
    stop_price = Decimal(str(trade_intent.stop_price))
    target_price = Decimal(str(trade_intent.target_price))
    side = SignalSide(trade_intent.side)
    favorable = side == SignalSide.BUY

    activation_fraction = (
        Decimal(str(trade_intent.trail_activation_fraction))
        if trade_intent.trail_activation_fraction is not None
        else TRAIL_ACTIVATION_FRACTION
    )
    lock_fraction = (
        Decimal(str(trade_intent.trail_lock_fraction))
        if trade_intent.trail_lock_fraction is not None
        else TRAIL_LOCK_FRACTION
    )
    activation_distance = abs(target_price - entry_price) * activation_fraction
    activation_price = (
        entry_price + activation_distance if favorable else entry_price - activation_distance
    )
    trail_stop: Decimal | None = None

    base = ReconstructedTrade(
        symbol=symbol,
        side=side.value,
        entry_time=entry_time,
        entry_price=float(entry_price),
        exit_time=None,
        exit_price=None,
        exit_reason="no_further_data",
        qty_lots=trade_intent.qty_lots,
        lot_size=lot_size,
    )

    def _exit(ts: datetime, price: Decimal, reason: str) -> ReconstructedTrade:
        base.exit_time, base.exit_price, base.exit_reason = ts, float(price), reason
        return _with_pnl(base, side)

    for bar in option_bars:
        if bar.ts < entry_time:
            continue
        price = Decimal(str(bar.close))

        if bar.ts.time() >= EOD_CUTOFF:
            return _exit(bar.ts, price, "eod_square_off")

        hit_stop = price <= stop_price if favorable else price >= stop_price
        if hit_stop:
            return _exit(bar.ts, stop_price, "stop")

        hit_target = price >= target_price if favorable else price <= target_price
        if hit_target:
            return _exit(bar.ts, target_price, "target")

        activated = price >= activation_price if favorable else price <= activation_price
        if activated:
            gain_beyond = (price - activation_price) if favorable else (activation_price - price)
            locked_gain = gain_beyond * lock_fraction
            new_trail_stop = (
                activation_price + locked_gain if favorable else activation_price - locked_gain
            )
            if trail_stop is None or (
                new_trail_stop > trail_stop if favorable else new_trail_stop < trail_stop
            ):
                trail_stop = new_trail_stop
            hit_trail = price < trail_stop if favorable else price > trail_stop
            if hit_trail:
                return _exit(bar.ts, trail_stop, "trail")

    return base


def _with_pnl(trade: ReconstructedTrade, side: SignalSide) -> ReconstructedTrade:
    if trade.exit_price is None:
        return trade
    sign = 1 if side == SignalSide.BUY else -1
    trade.pnl = (trade.exit_price - trade.entry_price) * trade.lot_size * trade.qty_lots * sign
    return trade


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _print_report(
    trades: list[ReconstructedTrade],
    risk_rejected_count: int,
    risk_rejected_reasons: Counter[str],
    total_signals: int,
) -> None:
    print()
    print("=" * 78)
    print("TRADE LOG")
    print("=" * 78)
    if not trades:
        print("(no signals reached risk-approval)")
    for t in trades:
        # entry_time round-trips through Postgres (trade_intent.created_at),
        # which normalizes tzinfo to UTC on read-back; exit_time never
        # leaves this process (built straight from an in-memory Bar.ts,
        # still IST-tagged). to_ist() is a no-op on an already-IST datetime
        # (see its own docstring), so applying it to both uniformly is what
        # makes the two columns display in the same, correct wall-clock zone
        # regardless of which one happened to round-trip through the DB.
        entry_str = to_ist(t.entry_time).strftime("%Y-%m-%d %H:%M")
        exit_str = to_ist(t.exit_time).strftime("%Y-%m-%d %H:%M") if t.exit_time else "UNRESOLVED"
        pnl_str = f"{t.pnl:+.2f}" if t.pnl is not None else "n/a"
        exit_price_str = f"{t.exit_price:.2f}" if t.exit_price is not None else "n/a"
        print(
            f"{entry_str}  {t.symbol:<22} {t.side:<4} "
            f"entry={t.entry_price:>9.2f}  exit={exit_price_str:>9} "
            f"@ {exit_str:<16} [{t.exit_reason:<14}]  pnl={pnl_str}"
        )

    resolved = [t for t in trades if t.pnl is not None]
    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"Signals risk-approved and reconstructed: {len(trades)}")
    print(f"Signals risk-rejected: {risk_rejected_count}")
    if risk_rejected_reasons:
        print("  Top rejection reasons:")
        for reason, count in risk_rejected_reasons.most_common(10):
            print(f"    {reason}: {count}")
    print(f"Total evaluate() cycles that produced a signal: {total_signals}")
    print(f"Resolved trades (exit found): {len(resolved)}")
    print(f"Unresolved trades (ran out of option data): {len(trades) - len(resolved)}")

    if not resolved:
        print("\nNo resolved trades — no PnL/win-rate metrics to report.")
        return

    wins = [t for t in resolved if t.pnl and t.pnl > 0]
    losses = [t for t in resolved if t.pnl and t.pnl <= 0]
    total_pnl = sum(t.pnl for t in resolved if t.pnl is not None)
    win_rate = len(wins) / len(resolved) * 100
    gross_win = sum(t.pnl for t in wins if t.pnl is not None)
    gross_loss = abs(sum(t.pnl for t in losses if t.pnl is not None))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for t in sorted(resolved, key=lambda x: x.entry_time):
        cumulative += t.pnl or 0.0
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    print(f"\nWin rate: {win_rate:.1f}% ({len(wins)}/{len(resolved)})")
    print(f"Total PnL: {total_pnl:+.2f}")
    print(f"Gross win / gross loss: {gross_win:.2f} / -{gross_loss:.2f}")
    profit_factor_str = (
        f"{profit_factor:.2f}" if profit_factor != float("inf") else "inf (no losses)"
    )
    print(f"Profit factor: {profit_factor_str}")
    print(f"Max drawdown: {max_drawdown:.2f}")

    print("\nExit reason breakdown:")
    for reason, count in Counter(t.exit_reason for t in resolved).most_common():
        print(f"  {reason}: {count}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", required=True, choices=STRATEGY_TYPES)
    parser.add_argument("--underlying", required=True, choices=sorted(UNDERLYING_META))
    parser.add_argument("--from", dest="from_date", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="to_date", required=True, type=date.fromisoformat)
    parser.add_argument("--expiry", type=date.fromisoformat, default=None)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()

    if args.to_date < args.from_date:
        raise SystemExit("--to must not be before --from")

    data_dir: Path = args.data_dir
    if not data_dir.is_dir():
        raise SystemExit(
            f"Historical data directory not found: {data_dir}\n"
            "Run scripts/fetch_truedata_historical.py first."
        )

    print(f"Loading underlying bars for {args.underlying} from {data_dir} ...")
    all_underlying_bars = _load_underlying_bars(data_dir, args.underlying)
    warmup_bars = [b for b in all_underlying_bars if b.ts.date() < args.from_date]
    main_bars = [b for b in all_underlying_bars if args.from_date <= b.ts.date() <= args.to_date]
    if not main_bars:
        raise SystemExit(
            f"No underlying bars in [{args.from_date.isoformat()}, {args.to_date.isoformat()}] "
            f"— available range is [{all_underlying_bars[0].ts.date()}, "
            f"{all_underlying_bars[-1].ts.date()}]"
        )
    print(
        f"{len(warmup_bars)} warm-up bars (indicators only), "
        f"{len(main_bars)} bars in the replay window"
    )

    expiry_date, expiry_dir = _discover_expiry_dir(
        data_dir, args.underlying, args.from_date, args.expiry
    )
    print(f"Using expiry {expiry_date.isoformat()} ({expiry_dir})")
    contracts, option_bars = _load_option_bars(
        args.underlying, expiry_dir, args.from_date, args.to_date
    )
    print(f"Loaded {len(contracts)} option contracts with data in the replay window")

    _ensure_backtest_database_exists()
    engine = create_engine(_backtest_database_url(), future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    @contextmanager
    def db_scope():
        db = session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    ctx = _seed_backtest_entities(
        db_scope,
        underlying=args.underlying,
        strategy_type=args.strategy,
        expiry_date=expiry_date,
        contracts=contracts,
        from_date=args.from_date,
    )

    reset_for_tests()
    historical_broker = HistoricalBrokerAdapter(contracts, option_bars)
    set_broker(historical_broker)

    # An unpersisted StrategyConfig, never added to any session -- reuses
    # api.v1.strategies._build_strategy's real strategy_type -> class
    # dispatch + PARAM_KEYS filtering (the exact production mapping) rather
    # than duck-typing a stand-in object mypy can't check against its real
    # `StrategyConfig` parameter type.
    strategy_config_stub = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=ctx.workspace_id,
        name="backtest-stub",
        strategy_type=args.strategy,
        params={},
    )
    strategy_obj = _build_strategy(strategy_config_stub, ctx.instrument_id, expiry_date)

    indicator_engine = IndicatorEngine()
    vwap_calc = VWAPCalculator()
    current_day: date | None = None
    total_signals = 0
    approved_trade_intent_ids: list[uuid.UUID] = []
    symbol_by_intent: dict[uuid.UUID, str] = {}

    print("Replaying bars through the real strategy pipeline ...")
    all_bars = sorted(warmup_bars + main_bars, key=lambda b: b.ts)
    for i, bar in enumerate(all_bars):
        bar_day = bar.ts.date()
        if bar_day != current_day:
            vwap_calc.reset()
            current_day = bar_day

        with db_scope() as db:
            _persist_underlying_bar(db, ctx.instrument_id, bar, indicator_engine, vwap_calc)

        if bar_day < args.from_date:
            continue  # warm-up only — indicators primed, no strategy evaluation

        simulated_time = bar.ts + timedelta(seconds=60)

        with db_scope() as db:
            db.query(OptionChainSnapshotRow).filter(
                OptionChainSnapshotRow.instrument_id == ctx.instrument_id,
                OptionChainSnapshotRow.expiry_date == expiry_date,
            ).delete()

        historical_broker.set_simulated_time(simulated_time)

        with db_scope() as db:
            strategy_run = db.get(StrategyRun, ctx.strategy_run_id)
            trading_session = db.get(TradingSession, ctx.trading_session_id)
            strategy_config = db.get(StrategyConfig, ctx.strategy_config_id)
            assert strategy_run is not None and trading_session is not None
            assert strategy_config is not None
            decision = run_cycle(db, strategy_obj, strategy_run, trading_session, strategy_config)
            if decision is not None:
                total_signals += 1
                _correct_timestamps(db, decision.trade_intent_id, simulated_time)
                trade_intent = db.get(TradeIntent, decision.trade_intent_id)
                is_approved = (
                    trade_intent is not None
                    and trade_intent.status == TradeIntentStatus.PENDING_APPROVAL
                )
                if is_approved and trade_intent is not None:
                    option_contract = db.get(OptionContract, trade_intent.option_contract_id)
                    if option_contract is not None:
                        approved_trade_intent_ids.append(trade_intent.id)
                        symbol_by_intent[trade_intent.id] = option_contract.symbol

        if (i + 1) % 500 == 0:
            print(f"  ... {i + 1}/{len(all_bars)} bars processed")

    print(
        f"Replay complete: {total_signals} signal(s) generated, "
        f"{len(approved_trade_intent_ids)} risk-approved"
    )

    risk_rejected_count = 0
    risk_rejected_reasons: Counter[str] = Counter()
    trades: list[ReconstructedTrade] = []
    with db_scope() as db:
        all_intents = (
            db.query(TradeIntent)
            .filter(TradeIntent.strategy_run_id == ctx.strategy_run_id)
            .order_by(TradeIntent.created_at)
            .all()
        )
        for ti in all_intents:
            if ti.status != TradeIntentStatus.RISK_REJECTED:
                continue
            risk_rejected_count += 1
            for decision in db.query(RiskDecision).filter(RiskDecision.trade_intent_id == ti.id):
                for reason in decision.reasons or []:
                    risk_rejected_reasons[reason] += 1

        for intent_id in approved_trade_intent_ids:
            trade_intent = db.get(TradeIntent, intent_id)
            if trade_intent is None:
                continue
            symbol = symbol_by_intent[intent_id]
            bars = option_bars.get(symbol, [])
            trades.append(_reconstruct_exit(trade_intent, symbol, bars, ctx.lot_size))

    _print_report(trades, risk_rejected_count, risk_rejected_reasons, total_signals)


if __name__ == "__main__":
    main()
