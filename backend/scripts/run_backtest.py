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

Every `StrategyRun` here uses `execution_mode=APPROVAL_REQUIRED` — but
**this does not actually gate anything in this backtest** (2026-08-23
correction, found live while validating today's changes): every
`TradingSession` this script seeds is `paper_only`, and
`risk_engine.service.evaluate_trade_intent`'s mode-aware rule (added
2026-08-19, postdating this script's original design and assumption
below) auto-dispatches every paper trade "regardless of the strategy's
configured execution_mode" (see that function's own comment). So a
risk-approved signal here reaches `TradeIntentStatus.DISPATCHED`, not
`PENDING_APPROVAL` as originally assumed — which DOES call
`execution_engine.paper.service.dispatch_trade_intent`, which DOES create
a real `Position`/`Order`/`StopPlan`/`TrailPlan` row (stamped with real
wall-clock time, since nothing in this script corrects those rows'
timestamps the way `_correct_timestamps` does for `TradeIntent`/`Signal`/
`RiskDecision`). This is harmless for this script's own results — nothing
below ever reads those four tables, `PositionManager` is never started,
and `_reconstruct_exit`'s own hand-rolled forward-walk uses only
`TradeIntent`'s (correctly, simulated-time-corrected) fields — but it left
stray real-shaped rows with wrong timestamps sitting in the backtest DB,
and, more importantly, an earlier version of this script's own
`is_approved` check (`status == PENDING_APPROVAL` only) silently treated
every one of these as "not approved", discarding every real trade and
always reporting zero — fixed by accepting both statuses as "risk
approved" below. This script's own `_reconstruct_exit` walks the saved
option-contract bars forward from each approved `TradeIntent`'s entry
point, applying the *same* stop/target/trail math
`execution_engine.paper.service.evaluate_open_position` uses (steps 1, 2,
5 of that function — see its own docstring), to compute a realized exit —
this part of the design is unaffected by the above, since it never
depended on which status name reflected "approved".

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
- **Bid/ask and depth are synthetic, derived from OI/volume as a liquidity
  proxy** (2026-08-23, explicit user decision — "arbitrary but consistent"
  rather than dropping the spread/depth scoring components entirely, since
  TrueData's historical REST endpoint returns OHLCV+OI only, never a real
  bid/ask or order-book depth). Within each cycle's chain snapshot,
  `HistoricalBrokerAdapter.get_option_chain` computes a 0..1
  `liquidity_score` per contract (0.5 * normalized OI + 0.5 * normalized
  volume, min-max across that cycle's contracts — the same `_normalize`
  shape `strike_ranking.engine` itself uses) and derives both from it:
  `spread_pct` interpolates between `MIN_SYNTHETIC_SPREAD_PCT` (tightest,
  most-liquid strike) and `MAX_SYNTHETIC_SPREAD_PCT` (widest, least-liquid)
  — replacing the old flat +/-0.25% half-spread — and `depth_qty` scales
  the same score up to `MAX_SYNTHETIC_DEPTH_QTY`, written as a real
  `DepthSnapshotRow` per contract (deleted and reinserted every cycle,
  mirroring the existing `OptionChainSnapshotRow` forced-refresh pattern
  below) so `rank_from_latest_snapshot`'s existing depth query picks it up
  unmodified — production ranking code itself is untouched, only this
  script's own data fabrication changed. A real spread/real order book
  would change which strike gets picked some of the time; this is a
  best-effort stand-in, not a claim of precision.
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
from app.domain.market.models import DepthSnapshot as DepthSnapshotRow  # noqa: E402
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
from app.modules.strategy_engine import runner as _runner_module  # noqa: E402
from app.modules.strategy_engine.runner import run_cycle  # noqa: E402

# The stalled-feed watchdog inside run_cycle (see runner.py's own docstring)
# compares real wall-clock time against real IST market hours before firing --
# a production safety check that is meaningless during a historical replay
# (it would compare "real now" against a simulated past date by construction,
# every single time it happens to run during actual market hours). Left live,
# 2026-08-24 found it adds real DB-write overhead on every bar with an open
# position once real wall-clock crosses 08:30 IST -- a measured ~3.6x
# slowdown on a live --all-expiries run (13/52 expiries in 2h15m vs. 24/52 in
# 1h10m before that boundary). `runner.py` binds `is_within_market_hours` into
# its own module namespace at import time (`from ... import
# is_within_market_hours`), so the patch target is this module's own
# attribute, not the original market_hours module.
_runner_module.is_within_market_hours = lambda: False

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

# Synthetic spread/depth proxy (2026-08-23, see module docstring's "Known,
# deliberate approximations" section) -- an arbitrary-but-documented range,
# not derived from any real observed spread distribution (none exists in
# this OHLCV+OI-only historical data to calibrate against).
MIN_SYNTHETIC_SPREAD_PCT = 0.0015  # tightest spread, most-liquid strike
MAX_SYNTHETIC_SPREAD_PCT = 0.025  # widest spread, least-liquid strike
MAX_SYNTHETIC_DEPTH_QTY = 2000  # depth_score saturates at 1000 (see engine.py)


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
    data_dir: Path,
    underlying: str,
    from_date: date,
    override: date | None,
    options_subdir: str = "options",
) -> tuple[date, Path]:
    base = data_dir / options_subdir / underlying
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
        *,
        db_scope: DbScope | None = None,
        contract_id_by_symbol: dict[str, uuid.UUID] | None = None,
    ) -> None:
        self._contracts = contracts
        self._option_bars = option_bars
        self._option_ts_index: dict[str, list[datetime]] = {
            symbol: [b.ts for b in bars] for symbol, bars in option_bars.items()
        }
        self._simulated_now: datetime | None = None
        # Both optional so this adapter still works standalone in isolation
        # (e.g. a unit test) -- depth synthesis is simply skipped without
        # them, same "optional dependency, degrade gracefully" shape the
        # module docstring's bid/ask/underlying_price params already use
        # elsewhere in this codebase.
        self._db_scope = db_scope
        self._contract_id_by_symbol = contract_id_by_symbol or {}
        self.last_pcr: float | None = None
        # 2026-08-24 perf variant ("fast" mode, see `_run_single_backtest`'s
        # own `fast` param): when the caller has an already-open
        # transaction it wants the depth write folded into (instead of
        # `_write_synthetic_depth` opening its own extra one via
        # `self._db_scope()`), it hands that session over here right before
        # calling `run_cycle` and clears it right after. `None` (the
        # default, unchanged for every existing caller) preserves the
        # original always-open-a-fresh-session behavior exactly.
        self._current_db: Session | None = None

    def set_current_db(self, db: Session | None) -> None:
        self._current_db = db

    def set_simulated_time(self, ts: datetime) -> None:
        self._simulated_now = ts

    def get_option_chain(self, underlying: str, expiry: date) -> OptionChainSnapshot:
        if self._simulated_now is None:
            raise RuntimeError("set_simulated_time() must be called before get_option_chain()")
        raw: list[tuple[str, float, ContractOptionType, Bar]] = []
        for symbol, strike, option_type in self._contracts:
            ts_list = self._option_ts_index.get(symbol)
            if not ts_list:
                continue
            idx = bisect_right(ts_list, self._simulated_now) - 1
            if idx < 0:
                continue  # this contract has no data yet as of this simulated moment
            raw.append((symbol, strike, option_type, self._option_bars[symbol][idx]))

        # OI/volume-derived liquidity proxy (see module docstring) -- min-max
        # normalized across this cycle's own live contracts, same convention
        # strike_ranking.engine._normalize already uses.
        ois = [b.oi for _, _, _, b in raw]
        volumes = [b.volume for _, _, _, b in raw]
        oi_lo, oi_hi = (min(ois), max(ois)) if ois else (0, 0)
        vol_lo, vol_hi = (min(volumes), max(volumes)) if volumes else (0, 0)

        def _liquidity_score(oi: int, volume: int) -> float:
            oi_n = 1.0 if oi_hi == oi_lo else (oi - oi_lo) / (oi_hi - oi_lo)
            vol_n = 1.0 if vol_hi == vol_lo else (volume - vol_lo) / (vol_hi - vol_lo)
            return 0.5 * oi_n + 0.5 * vol_n

        entries: list[OptionChainEntry] = []
        depth_by_symbol: dict[str, int] = {}
        total_ce_oi = 0
        total_pe_oi = 0
        for symbol, strike, option_type, bar in raw:
            liquidity = _liquidity_score(bar.oi, bar.volume)
            spread_pct = (
                MAX_SYNTHETIC_SPREAD_PCT
                - liquidity * (MAX_SYNTHETIC_SPREAD_PCT - MIN_SYNTHETIC_SPREAD_PCT)
            )
            half_spread = bar.close * spread_pct / 2
            depth_by_symbol[symbol] = round(liquidity * MAX_SYNTHETIC_DEPTH_QTY)
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
            if option_type == ContractOptionType.CE:
                total_ce_oi += bar.oi
            else:
                total_pe_oi += bar.oi

        self.last_pcr = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else None
        self._write_synthetic_depth(depth_by_symbol)

        # ts is real wall-clock `now`, deliberately not the simulated time —
        # see module docstring's "option-chain freshness gate" section for
        # why: this snapshot is always freshly deleted-then-refetched by the
        # caller immediately before use, so its own ts only needs to satisfy
        # ensure_fresh_option_chain's real-wall-clock LIVE check, not carry
        # simulated time anywhere meaningful.
        return OptionChainSnapshot(
            underlying=underlying, expiry=expiry, ts=datetime.now(UTC), entries=tuple(entries)
        )

    def _write_synthetic_depth(self, depth_by_symbol: dict[str, int]) -> None:
        """Delete-then-reinsert per cycle, mirroring the caller's own
        OptionChainSnapshotRow forced-refresh pattern (see module
        docstring) -- keeps row count bounded (one live row per contract)
        rather than growing unbounded across a year-long replay.

        Two modes, both correct, differing only in commit count (a
        2026-08-24 perf change, added *after* NIFTY EMA/ORB's original
        full-year runs already started on the always-fresh-session path
        below -- see `_run_single_backtest`'s own `fast` param docstring
        for the full reasoning and the smoke-test comparison that verified
        both modes produce byte-identical trades before this was trusted
        on a full run): if `set_current_db` handed us an active session
        (`self._current_db` set), write into it directly -- no extra
        commit, since it's the same transaction `run_cycle` itself is
        already mid-way through, and Postgres read-committed isolation
        means that transaction's own later query already sees its own
        uncommitted write regardless. Otherwise (the original, still-
        default behavior every existing caller gets unchanged), open a
        dedicated short-lived session/commit of our own, distinct from
        whatever transaction `run_cycle` is mid-way through, relying on
        read-committed isolation the same way once *that* commits.
        """
        if not self._contract_id_by_symbol:
            return
        contract_ids = list(self._contract_id_by_symbol.values())
        now = datetime.now(UTC)

        def _write(db: Session) -> None:
            db.query(DepthSnapshotRow).filter(
                DepthSnapshotRow.option_contract_id.in_(contract_ids)
            ).delete(synchronize_session=False)
            for symbol, qty in depth_by_symbol.items():
                contract_id = self._contract_id_by_symbol.get(symbol)
                if contract_id is None:
                    continue
                half = qty // 2
                db.add(
                    DepthSnapshotRow(
                        id=uuid.uuid4(),
                        option_contract_id=contract_id,
                        ts=now,
                        bid_levels=[{"qty": half}],
                        ask_levels=[{"qty": qty - half}],
                    )
                )

        if self._current_db is not None:
            _write(self._current_db)
            self._current_db.flush()
            return
        if self._db_scope is None:
            return
        with self._db_scope() as db:
            _write(db)

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


def _backtest_db_name(suffix: str) -> str:
    """`suffix` (default: `--strategy`, see `main()`) keeps concurrent runs
    of this script fully isolated -- 2026-08-23, found live: `main()` does
    `Base.metadata.drop_all`/`create_all` against this database at the
    start of every invocation, so two runs sharing one `<DB_NAME>_backtest`
    database (e.g. a second strategy started while the first is still
    replaying) would have the second run's startup DROP the first run's
    in-progress tables out from under it. A suffixed, per-run database name
    makes that structurally impossible rather than relying on "don't run
    two at once" discipline.
    """
    return f"{get_settings().db.name}_backtest_{suffix}"


def _backtest_database_url(suffix: str) -> str:
    base_url = get_settings().db.sqlalchemy_url.rsplit("/", 1)[0]
    return f"{base_url}/{_backtest_db_name(suffix)}"


def _ensure_backtest_database_exists(suffix: str) -> None:
    """Mirrors tests/conftest.py's `_ensure_test_database_exists` exactly —
    a dedicated `<DB_NAME>_backtest_<suffix>` database, never `DB_NAME`/
    `DB_NAME_test` directly, created on demand via a maintenance connection
    to `postgres`.
    """
    maintenance_url = get_settings().db.sqlalchemy_url.rsplit("/", 1)[0] + "/postgres"
    maintenance_engine = create_engine(maintenance_url, future=True, isolation_level="AUTOCOMMIT")
    try:
        with maintenance_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": _backtest_db_name(suffix)},
            ).first()
            if exists is None:
                conn.execute(text(f'CREATE DATABASE "{_backtest_db_name(suffix)}"'))
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

        # A dedicated Instrument per expiry run, not a shared one reused
        # across expiries: `Instrument.symbol/exchange` has a global unique
        # constraint (`uq_instrument_symbol_exchange`), so `--all-expiries`
        # calling this once per expiry with a bare `underlying` symbol
        # collides on the second call (2026-08-23, first fix attempt).
        # Reusing one shared Instrument row across expiries was tried next
        # and rejected: `price_bars`/`quote_ticks`/`indicator_snapshots`
        # are keyed by `(instrument_id, ...)`, and adjacent expiries'
        # replay windows genuinely overlap in calendar days (warm-up reaches
        # back, and consecutive weekly/monthly option windows share trading
        # days) — a shared instrument_id then hits
        # `uq_price_bar_bucket` on the second expiry's overlapping bar.
        # Each expiry therefore gets its own fully isolated Instrument (and
        # thus its own price_bars/indicator rows), keyed by an
        # expiry-suffixed symbol so it never collides with another expiry's
        # — matching the same "every expiry run is fully independent"
        # isolation every other seeded row here already has.
        instrument = Instrument(
            id=uuid.uuid4(),
            symbol=f"{underlying}~{expiry_date.isoformat()}",
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
    # Diagnostic-only barometers (2026-08-23, explicit user request): recorded
    # at entry/exit for post-hoc analysis, never fed into any strategy's own
    # entry/exit condition -- see module docstring. None wherever the
    # underlying data source doesn't cover that moment (e.g. VIX 1-min
    # outside its own real ~15-day window falls back to that day's EOD
    # close; still None if even that's missing).
    vix_entry: float | None = None
    vix_exit: float | None = None
    atr_entry: float | None = None
    atr_exit: float | None = None
    pcr_entry: float | None = None
    pcr_exit: float | None = None
    contract_oi_entry: int | None = None
    contract_oi_exit: int | None = None


class ATRTracker:
    """Simple rolling ATR (Wilder-style smoothing) over a sequential stream
    of underlying `Bar`s -- diagnostic-only (see `ReconstructedTrade`'s own
    docstring), not read by any strategy's entry/exit logic. Returns `None`
    until `period` bars have been seen.
    """

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._prev_close: float | None = None
        self._values: list[float] = []
        self._atr: float | None = None

    def update(self, bar: Bar) -> float | None:
        if self._prev_close is None:
            true_range = bar.high - bar.low
        else:
            true_range = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close

        if self._atr is None:
            self._values.append(true_range)
            if len(self._values) >= self.period:
                self._atr = sum(self._values) / self.period
        else:
            self._atr = (self._atr * (self.period - 1) + true_range) / self.period
        return self._atr


def _load_minute_series(path: Path) -> list[tuple[datetime, float]]:
    if not path.is_file():
        return []
    series = [(b.ts, b.close) for b in _load_csv_bars(path)]
    series.sort(key=lambda pair: pair[0])
    return series


def _load_daily_series(path: Path) -> list[tuple[date, float]]:
    if not path.is_file():
        return []
    import csv as _csv  # noqa: PLC0415

    rows: list[tuple[date, float]] = []
    with path.open(newline="") as f:
        for row in _csv.DictReader(f):
            rows.append((date.fromisoformat(row["timestamp"][:10]), float(row["close"])))
    rows.sort(key=lambda pair: pair[0])
    return rows


def _lookup_nearest_minute(series: list[tuple[datetime, float]], ts: datetime) -> float | None:
    if not series:
        return None
    idx = bisect_right([s[0] for s in series], ts) - 1
    return series[idx][1] if idx >= 0 else None


def _lookup_nearest_daily(series: list[tuple[date, float]], on: date) -> float | None:
    if not series:
        return None
    idx = bisect_right([s[0] for s in series], on) - 1
    return series[idx][1] if idx >= 0 else None


class DiagnosticsSource:
    """VIX (1-min where available, else that day's EOD close) + PCR lookups
    for the diagnostic-only barometers on `ReconstructedTrade` -- see that
    class's own docstring. ATR is tracked separately (`ATRTracker`, a
    sequential stream, not point-lookup) since it needs the full bar
    history up to a point, not just one value at a timestamp.
    """

    def __init__(self, data_dir: Path) -> None:
        self.vix_minute = _load_minute_series(data_dir / "underlyings" / "INDIA_VIX_1min.csv")
        self.vix_daily = _load_daily_series(data_dir / "underlyings_eod" / "INDIA_VIX_eod.csv")

    def vix_at(self, ts: datetime) -> float | None:
        value = _lookup_nearest_minute(self.vix_minute, ts)
        if value is not None:
            return value
        return _lookup_nearest_daily(self.vix_daily, ts.date())


def _pcr_at(
    contracts: list[tuple[str, float, ContractOptionType]],
    option_bars: dict[str, list[Bar]],
    ts: datetime,
) -> float | None:
    """Aggregate PE-OI/CE-OI across every contract in this expiry's chain,
    as of the most recent bar at-or-before `ts` for each contract — the
    same "genuine chain-wide OI ratio" PCR is supposed to measure, not just
    the traded contract's own OI. Diagnostic-only, mirrors
    `HistoricalBrokerAdapter.get_option_chain`'s own per-cycle PCR
    computation (used there for entry-time; this is the standalone version
    `_reconstruct_exit` uses for exit-time, since exit reconstruction runs
    after the main replay loop, with no live `HistoricalBrokerAdapter`
    cycle to read `last_pcr` off).
    """
    total_ce_oi = 0
    total_pe_oi = 0
    for symbol, _strike, option_type in contracts:
        bars = option_bars.get(symbol)
        if not bars:
            continue
        ts_list = [b.ts for b in bars]
        idx = bisect_right(ts_list, ts) - 1
        if idx < 0:
            continue
        oi = bars[idx].oi
        if option_type == ContractOptionType.CE:
            total_ce_oi += oi
        else:
            total_pe_oi += oi
    return (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else None


def _contract_oi_at(bars: list[Bar], ts: datetime) -> int | None:
    ts_list = [b.ts for b in bars]
    idx = bisect_right(ts_list, ts) - 1
    return bars[idx].oi if idx >= 0 else None


def _reconstruct_exit(
    trade_intent: TradeIntent,
    symbol: str,
    option_bars: list[Bar],
    lot_size: int,
    *,
    entry_diagnostics: dict[str, float | int | None] | None = None,
    diagnostics: DiagnosticsSource | None = None,
    all_contracts: list[tuple[str, float, ContractOptionType]] | None = None,
    all_option_bars: dict[str, list[Bar]] | None = None,
    atr_series: list[tuple[datetime, float]] | None = None,
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

    entry_diagnostics = entry_diagnostics or {}
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
        vix_entry=entry_diagnostics.get("vix"),  # type: ignore[arg-type]
        atr_entry=entry_diagnostics.get("atr"),  # type: ignore[arg-type]
        pcr_entry=entry_diagnostics.get("pcr"),  # type: ignore[arg-type]
        contract_oi_entry=entry_diagnostics.get("contract_oi"),  # type: ignore[arg-type]
    )

    def _exit(ts: datetime, price: Decimal, reason: str) -> ReconstructedTrade:
        base.exit_time, base.exit_price, base.exit_reason = ts, float(price), reason
        if diagnostics is not None:
            base.vix_exit = diagnostics.vix_at(ts)
        if all_contracts is not None and all_option_bars is not None:
            base.pcr_exit = _pcr_at(all_contracts, all_option_bars, ts)
        if atr_series is not None:
            base.atr_exit = _lookup_nearest_minute(atr_series, ts)
        base.contract_oi_exit = _contract_oi_at(option_bars, ts)
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


def _write_trade_csv(trades: list[ReconstructedTrade], out_path: Path) -> None:
    """Full trade log incl. diagnostic-only VIX/ATR/PCR/contract-OI columns
    (2026-08-23, explicit user request — for post-hoc strategy analysis,
    never fed back into any strategy's own logic; see `ReconstructedTrade`'s
    own docstring). The console report above stays a summary; this is the
    complete, per-trade record.
    """
    import csv as _csv  # noqa: PLC0415

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = _csv.writer(f)
        writer.writerow(
            [
                "symbol", "side", "entry_time", "entry_price", "exit_time", "exit_price",
                "exit_reason", "qty_lots", "lot_size", "pnl",
                "vix_entry", "vix_exit", "atr_entry", "atr_exit",
                "pcr_entry", "pcr_exit", "contract_oi_entry", "contract_oi_exit",
            ]
        )
        for t in sorted(trades, key=lambda x: x.entry_time):
            writer.writerow(
                [
                    t.symbol, t.side, to_ist(t.entry_time).isoformat(), t.entry_price,
                    to_ist(t.exit_time).isoformat() if t.exit_time else "",
                    t.exit_price if t.exit_price is not None else "",
                    t.exit_reason, t.qty_lots, t.lot_size,
                    t.pnl if t.pnl is not None else "",
                    t.vix_entry if t.vix_entry is not None else "",
                    t.vix_exit if t.vix_exit is not None else "",
                    t.atr_entry if t.atr_entry is not None else "",
                    t.atr_exit if t.atr_exit is not None else "",
                    t.pcr_entry if t.pcr_entry is not None else "",
                    t.pcr_exit if t.pcr_exit is not None else "",
                    t.contract_oi_entry if t.contract_oi_entry is not None else "",
                    t.contract_oi_exit if t.contract_oi_exit is not None else "",
                ]
            )
    print(f"\nFull trade log (with diagnostics) written to {out_path}")


_TRADE_CSV_HEADER = [
    "symbol", "side", "entry_time", "entry_price", "exit_time", "exit_price",
    "exit_reason", "qty_lots", "lot_size", "pnl",
    "vix_entry", "vix_exit", "atr_entry", "atr_exit",
    "pcr_entry", "pcr_exit", "contract_oi_entry", "contract_oi_exit",
]


def _append_trade_csv_rows(
    trades: list[ReconstructedTrade], out_path: Path, *, write_header: bool
) -> None:
    """Incremental counterpart to `_write_trade_csv`, called once per expiry
    during an `--all-expiries` run (2026-08-24, explicit user request) so a
    process killed mid-run (e.g. by `/clear` wiping its background task --
    a real incident, see project memory) still leaves every already-
    completed expiry's trades on disk, not just whatever the final
    end-of-run `_write_trade_csv` call would have written. `f.flush()` +
    `os.fsync` make each expiry's write durable immediately rather than
    sitting in an OS buffer a killed process could lose. The final
    `_write_trade_csv(all_trades, out_csv)` call at the end of `main()`
    still runs on a clean completion and overwrites this file with the
    fully sorted, canonical version -- this function only exists to make
    partial progress survivable, not to replace that.
    """
    import csv as _csv  # noqa: PLC0415
    import os  # noqa: PLC0415

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a" if not write_header else "w", newline="") as f:
        writer = _csv.writer(f)
        if write_header:
            writer.writerow(_TRADE_CSV_HEADER)
        for t in sorted(trades, key=lambda x: x.entry_time):
            writer.writerow(
                [
                    t.symbol, t.side, to_ist(t.entry_time).isoformat(), t.entry_price,
                    to_ist(t.exit_time).isoformat() if t.exit_time else "",
                    t.exit_price if t.exit_price is not None else "",
                    t.exit_reason, t.qty_lots, t.lot_size,
                    t.pnl if t.pnl is not None else "",
                    t.vix_entry if t.vix_entry is not None else "",
                    t.vix_exit if t.vix_exit is not None else "",
                    t.atr_entry if t.atr_entry is not None else "",
                    t.atr_exit if t.atr_exit is not None else "",
                    t.pcr_entry if t.pcr_entry is not None else "",
                    t.pcr_exit if t.pcr_exit is not None else "",
                    t.contract_oi_entry if t.contract_oi_entry is not None else "",
                    t.contract_oi_exit if t.contract_oi_exit is not None else "",
                ]
            )
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _expiry_data_range(expiry_dir: Path) -> tuple[date, date] | None:
    """True min/max trading date across every contract file in one expiry
    directory — scanned directly rather than assumed from the expiry date
    itself, since a contract's real listed life varies (see module
    docstring's data-limitation notes).
    """
    min_d: date | None = None
    max_d: date | None = None
    for path in expiry_dir.glob("*.csv"):
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        if not rows:
            continue
        first_d = datetime.strptime(rows[0]["timestamp"], "%Y-%m-%d %H:%M:%S").date()
        last_d = datetime.strptime(rows[-1]["timestamp"], "%Y-%m-%d %H:%M:%S").date()
        min_d = first_d if min_d is None or first_d < min_d else min_d
        max_d = last_d if max_d is None or last_d > max_d else max_d
    if min_d is None or max_d is None:
        return None
    return min_d, max_d


def _run_single_backtest(
    *,
    underlying: str,
    strategy_type: str,
    from_date: date,
    to_date: date,
    expiry_date: date,
    expiry_dir: Path,
    all_underlying_bars: list[Bar],
    diagnostics: DiagnosticsSource,
    session_factory: sessionmaker,
    quiet: bool = False,
    fast: bool = False,
) -> tuple[list[ReconstructedTrade], int, Counter[str], int]:
    """One expiry's full seed -> replay -> risk-outcome -> exit-reconstruction
    pass — the exact single-run body `main()` used to run inline, now
    reusable so `--all-expiries` can call it once per discovered expiry
    directory and aggregate. Returns
    (trades, risk_rejected_count, risk_rejected_reasons, total_signals).

    `fast` (2026-08-24, off by default -- every run before this date, and
    every run today that doesn't pass `--fast`, is completely unaffected):
    on each main-window bar, the original loop opens 4 separate DB
    transactions -- persist the underlying bar, delete the stale
    option-chain snapshot, `HistoricalBrokerAdapter`'s own extra commit for
    the synthetic depth rows, and `run_cycle` itself. `fast=True` merges
    these into 2: bar-persist + snapshot-delete share one transaction, and
    `historical_broker.set_current_db(db)` (cleared again right after) lets
    the depth write land inside `run_cycle`'s own transaction instead of
    opening a third. This changes nothing about *what* gets written or
    read or in what order -- Postgres read-committed isolation already
    guarantees a transaction sees its own prior writes, merge or no merge
    -- only how many round-trip commits it costs. Verified byte-identical
    against the non-fast path on a single-expiry smoke test (same entry/
    exit/pnl/exit_reason) before being trusted on any full `--all-expiries`
    run; see the project handover notes for that comparison's actual
    numbers, not just this claim.
    """
    contracts, option_bars = _load_option_bars(underlying, expiry_dir, from_date, to_date)
    if not quiet:
        print(f"  [{expiry_date.isoformat()}] {len(contracts)} contracts with data")

    # Capped, not "every bar since the start of the archive": EMA9/EMA20 and
    # the body-ratio/expansion lookbacks all stabilize within a few dozen
    # bars, and warm-up bars still get persisted (DB writes) for every bar
    # replayed -- an uncapped warm-up would make each successive expiry in
    # a year-long --all-expiries loop replay the entire prior history again,
    # making runtime grow roughly quadratically with the number of expiries.
    warmup_bars = [b for b in all_underlying_bars if b.ts.date() < from_date][-1000:]
    main_bars = [b for b in all_underlying_bars if from_date <= b.ts.date() <= to_date]
    if not main_bars:
        if not quiet:
            print(f"  [{expiry_date.isoformat()}] no underlying bars in window, skipping")
        return [], 0, Counter(), 0

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
        underlying=underlying,
        strategy_type=strategy_type,
        expiry_date=expiry_date,
        contracts=contracts,
        from_date=from_date,
    )
    contract_id_by_symbol: dict[str, uuid.UUID] = {}
    with db_scope() as db:
        for row in db.query(OptionContract).filter(
            OptionContract.instrument_id == ctx.instrument_id,
            OptionContract.expiry_date == expiry_date,
        ):
            contract_id_by_symbol[row.symbol] = row.id

    reset_for_tests()
    historical_broker = HistoricalBrokerAdapter(
        contracts, option_bars, db_scope=db_scope, contract_id_by_symbol=contract_id_by_symbol
    )
    set_broker(historical_broker)

    strategy_config_stub = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=ctx.workspace_id,
        name="backtest-stub",
        strategy_type=strategy_type,
        params={},
    )
    strategy_obj = _build_strategy(strategy_config_stub, ctx.instrument_id, expiry_date)

    indicator_engine = IndicatorEngine()
    vwap_calc = VWAPCalculator()
    atr_tracker = ATRTracker()
    atr_series: list[tuple[datetime, float]] = []
    current_day: date | None = None
    total_signals = 0
    approved_trade_intent_ids: list[uuid.UUID] = []
    symbol_by_intent: dict[uuid.UUID, str] = {}
    entry_diagnostics_by_intent: dict[uuid.UUID, dict[str, float | int | None]] = {}

    all_bars = sorted(warmup_bars + main_bars, key=lambda b: b.ts)
    for i, bar in enumerate(all_bars):
        bar_day = bar.ts.date()
        if bar_day != current_day:
            vwap_calc.reset()
            current_day = bar_day

        if fast and bar_day >= from_date:
            # Merged: bar-persist + stale-snapshot-delete share one
            # transaction instead of two (see `fast`'s own docstring above).
            with db_scope() as db:
                _persist_underlying_bar(db, ctx.instrument_id, bar, indicator_engine, vwap_calc)
                db.query(OptionChainSnapshotRow).filter(
                    OptionChainSnapshotRow.instrument_id == ctx.instrument_id,
                    OptionChainSnapshotRow.expiry_date == expiry_date,
                ).delete()
        else:
            with db_scope() as db:
                _persist_underlying_bar(db, ctx.instrument_id, bar, indicator_engine, vwap_calc)
        atr_value = atr_tracker.update(bar)
        if atr_value is not None:
            atr_series.append((bar.ts, atr_value))

        if bar_day < from_date:
            continue  # warm-up only — indicators primed, no strategy evaluation

        simulated_time = bar.ts + timedelta(seconds=60)

        if not fast:
            with db_scope() as db:
                db.query(OptionChainSnapshotRow).filter(
                    OptionChainSnapshotRow.instrument_id == ctx.instrument_id,
                    OptionChainSnapshotRow.expiry_date == expiry_date,
                ).delete()

        historical_broker.set_simulated_time(simulated_time)

        with db_scope() as db:
            if fast:
                historical_broker.set_current_db(db)
            strategy_run = db.get(StrategyRun, ctx.strategy_run_id)
            trading_session = db.get(TradingSession, ctx.trading_session_id)
            strategy_config = db.get(StrategyConfig, ctx.strategy_config_id)
            assert strategy_run is not None and trading_session is not None
            assert strategy_config is not None
            decision = run_cycle(
                db, strategy_obj, strategy_run, trading_session, strategy_config,
                alert_session_factory=session_factory,
            )
            if fast:
                historical_broker.set_current_db(None)
            if decision is not None:
                total_signals += 1
                _correct_timestamps(db, decision.trade_intent_id, simulated_time)
                trade_intent = db.get(TradeIntent, decision.trade_intent_id)
                # 2026-08-23 fix: this backtest DB is always paper_only, and
                # risk_engine.service.evaluate_trade_intent's mode-aware
                # rule (added 2026-08-19, postdating this script's original
                # design) auto-dispatches EVERY paper trade regardless of
                # execution_mode -- "paper trades always auto-dispatch,
                # regardless of the strategy's configured execution_mode"
                # (see that function's own comment). So a risk-approved
                # signal here reaches DISPATCHED, never PENDING_APPROVAL —
                # checking only PENDING_APPROVAL (as this script originally
                # did) silently discarded every real trade, always
                # reporting zero. Both statuses mean "risk approved this
                # trade"; DISPATCHED additionally means a real Position/
                # Order/StopPlan/TrailPlan row now exists with real
                # wall-clock (not simulated) timestamps — harmless here
                # since reconstruction below reads only TradeIntent's own
                # (already timestamp-corrected) fields, never those rows.
                is_approved = trade_intent is not None and trade_intent.status in (
                    TradeIntentStatus.PENDING_APPROVAL,
                    TradeIntentStatus.DISPATCHED,
                )
                if is_approved and trade_intent is not None:
                    option_contract = db.get(OptionContract, trade_intent.option_contract_id)
                    if option_contract is not None:
                        approved_trade_intent_ids.append(trade_intent.id)
                        symbol_by_intent[trade_intent.id] = option_contract.symbol
                        entry_diagnostics_by_intent[trade_intent.id] = {
                            "vix": diagnostics.vix_at(simulated_time),
                            "atr": atr_value,
                            "pcr": historical_broker.last_pcr,
                            "contract_oi": _contract_oi_at(
                                option_bars.get(option_contract.symbol, []), simulated_time
                            ),
                        }

        if not quiet and (i + 1) % 2000 == 0:
            print(f"    ... {i + 1}/{len(all_bars)} bars processed")

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
            trades.append(
                _reconstruct_exit(
                    trade_intent,
                    symbol,
                    bars,
                    ctx.lot_size,
                    entry_diagnostics=entry_diagnostics_by_intent.get(intent_id),
                    diagnostics=diagnostics,
                    all_contracts=contracts,
                    all_option_bars=option_bars,
                    atr_series=atr_series,
                )
            )

    if not quiet:
        print(
            f"  [{expiry_date.isoformat()}] {total_signals} signal(s), "
            f"{len(approved_trade_intent_ids)} risk-approved, {risk_rejected_count} rejected"
        )
    return trades, risk_rejected_count, risk_rejected_reasons, total_signals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", required=True, choices=STRATEGY_TYPES)
    parser.add_argument("--underlying", required=True, choices=sorted(UNDERLYING_META))
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat, default=None)
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat, default=None)
    parser.add_argument("--expiry", type=date.fromisoformat, default=None)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--options-subdir", default="options",
        help="Subdir under --data-dir to read option chains from "
        "('options' = current near-term chain, 'options_1min_past' = past-year archive)",
    )
    parser.add_argument(
        "--underlying-source", choices=("spot", "futures_proxy", "alice_index"), default="spot",
        help="'spot' = underlyings/<u>_1min.csv (real but ~12-day cap); "
        "'futures_proxy' = underlyings/<u>_underlying_proxy_1min.csv "
        "(stitched real monthly-futures history, real data only ~1wk/month near each "
        "contract's own expiry -- see fetch_truedata_futures_underlying_history.py); "
        "'alice_index' = underlyings/<u>_alice_index_1min.csv (real, continuous NSE-index "
        "1-min history via Alice Blue's historical chart API, ~3.2 years with zero gaps "
        "over 4 calendar days -- confirmed 2026-08-24, see "
        "fetch_alice_blue_underlying_history.py; the recommended source for any "
        "--all-expiries run over the options_1min_past archive)",
    )
    parser.add_argument(
        "--all-expiries", action="store_true",
        help="Ignore --from/--to/--expiry; replay every expiry directory found under "
        "--data-dir/--options-subdir/--underlying and aggregate results.",
    )
    parser.add_argument("--out-csv", type=Path, default=None)
    parser.add_argument(
        "--db-suffix", default=None,
        help="Backtest database name suffix (<DB_NAME>_backtest_<suffix>). Defaults to "
        "<strategy>_<underlying>, so different strategy/underlying combinations never "
        "collide when run concurrently.",
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="Merge per-bar DB transactions (4 -> 2) for less commit overhead. Same reads/"
        "writes/order, only fewer transaction boundaries -- verify with a single-expiry "
        "smoke test against a non-fast run before trusting on a full --all-expiries run.",
    )
    args = parser.parse_args()
    db_suffix = args.db_suffix or f"{args.strategy}_{args.underlying}"

    data_dir: Path = args.data_dir
    if not data_dir.is_dir():
        raise SystemExit(
            f"Historical data directory not found: {data_dir}\n"
            "Run scripts/fetch_truedata_historical.py first."
        )

    underlying_filename = {
        "spot": f"{args.underlying}_1min.csv",
        "futures_proxy": f"{args.underlying}_underlying_proxy_1min.csv",
        "alice_index": f"{args.underlying}_alice_index_1min.csv",
    }[args.underlying_source]
    underlying_path = data_dir / "underlyings" / underlying_filename
    if not underlying_path.is_file():
        raise SystemExit(f"Missing underlying data file: {underlying_path}")
    print(f"Loading underlying bars for {args.underlying} from {underlying_path} ...")
    all_underlying_bars = _load_csv_bars(underlying_path)
    print(f"{len(all_underlying_bars)} underlying bars loaded")

    diagnostics = DiagnosticsSource(data_dir)

    _ensure_backtest_database_exists(db_suffix)
    engine = create_engine(_backtest_database_url(db_suffix), future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    all_trades: list[ReconstructedTrade] = []
    total_risk_rejected = 0
    total_risk_rejected_reasons: Counter[str] = Counter()
    total_signals_all = 0

    out_csv = args.out_csv or (
        REPO_ROOT / "data" / "historical" / "backtest_reports"
        / f"{args.strategy}_{args.underlying}_trades.csv"
    )

    if args.all_expiries:
        options_base = data_dir / args.options_subdir / args.underlying
        if not options_base.is_dir():
            raise SystemExit(f"Missing option data directory: {options_base}")
        expiry_dirs = sorted(
            (d for d in options_base.iterdir() if d.is_dir()), key=lambda d: d.name
        )
        print(f"Replaying {len(expiry_dirs)} expiries for {args.strategy}/{args.underlying} ...")
        wrote_header = False
        for expiry_dir in expiry_dirs:
            expiry_date = date.fromisoformat(expiry_dir.name)
            date_range = _expiry_data_range(expiry_dir)
            if date_range is None:
                print(f"  [{expiry_date.isoformat()}] no data in any contract file, skipping")
                continue
            from_date, to_date = date_range
            trades, rejected, reasons, signals = _run_single_backtest(
                underlying=args.underlying,
                strategy_type=args.strategy,
                from_date=from_date,
                to_date=to_date,
                expiry_date=expiry_date,
                expiry_dir=expiry_dir,
                all_underlying_bars=all_underlying_bars,
                diagnostics=diagnostics,
                session_factory=session_factory,
                fast=args.fast,
            )
            all_trades.extend(trades)
            total_risk_rejected += rejected
            total_risk_rejected_reasons.update(reasons)
            total_signals_all += signals
            _append_trade_csv_rows(trades, out_csv, write_header=not wrote_header)
            wrote_header = True
            print(
                f"  [{expiry_date.isoformat()}] {len(trades)} trade(s) appended to {out_csv} "
                f"({len(all_trades)} total so far)"
            )
    else:
        if args.from_date is None or args.to_date is None:
            raise SystemExit("--from/--to are required unless --all-expiries is set")
        if args.to_date < args.from_date:
            raise SystemExit("--to must not be before --from")
        expiry_date, expiry_dir = _discover_expiry_dir(
            data_dir, args.underlying, args.from_date, args.expiry, args.options_subdir
        )
        print(f"Using expiry {expiry_date.isoformat()} ({expiry_dir})")
        all_trades, total_risk_rejected, total_risk_rejected_reasons, total_signals_all = (
            _run_single_backtest(
                underlying=args.underlying,
                strategy_type=args.strategy,
                from_date=args.from_date,
                to_date=args.to_date,
                expiry_date=expiry_date,
                expiry_dir=expiry_dir,
                all_underlying_bars=all_underlying_bars,
                diagnostics=diagnostics,
                session_factory=session_factory,
                quiet=False,
                fast=args.fast,
            )
        )

    _print_report(all_trades, total_risk_rejected, total_risk_rejected_reasons, total_signals_all)

    _write_trade_csv(all_trades, out_csv)


if __name__ == "__main__":
    main()
