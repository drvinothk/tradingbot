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

- **`legacy`/`near_only`/`far_only`/`no_target_only`/`split_30_30_40`/
  `target_mult` still don't model structure-break/spread-blowout exits and
  still use close-only pricing** (`_reconstruct_exit`/`_reconstruct_exit_
  legs`, deliberately untouched — see `legacy`'s own promise below). Use
  `--exit-mode current` for the faithful reconstruction; these other modes
  exist specifically as deliberate what-if *target*-mechanism substitutions
  (near/far pivot targets, no target, a split, a scaled target) layered on
  top of `legacy`'s own baseline mechanics, not faithful simulations in
  their own right — their PnL reads more optimistic than a real run's
  would, for exactly the reasons below.
- **`current` (2026-08-27, default) models all 5 real steps of
  `evaluate_open_position`** — stop, target, structure-break,
  spread-blowout, trail, in that exact production order — using each bar's
  high/low, not just its close (`_reconstruct_exit_current`). Two genuine,
  permanent limitations remain even here, since no historical source (this
  script's own or any other) offers finer than 1-min OHLCV bars: (1) every
  strategy's real `structure_break_persistence_seconds` default (6.0s) is
  always exceeded by a single 1-min bar, so the persistence timer collapses
  to "survives to the next completed bar" rather than production's real
  "survives ~2 live 3s poll cycles"; (2) same-bar ties (a bar's range
  satisfying more than one condition at once — impossible on a live tick,
  common at 1-min resolution) are resolved via production's own fixed
  check order (stop → target → structure-break → spread-blowout → trail),
  the conservative/loss-first reading, confirmed with the user rather than
  silently chosen.
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
  best-effort stand-in, not a claim of precision. `--exit-mode current`'s
  spread-blowout check (`_spread_pct_at`) reuses this exact same formula at
  exit-walk timestamps too (2026-08-27) — a **permanent** ceiling, not a
  temporary gap: no historical source available to this script (TrueData
  archive, Shoonya TPSeries, Alice Blue) carries real bid/ask/depth at any
  granularity. The only way to ever close this for real is a forward-only
  live-tick/quote capture pipeline (recording from today onward — doesn't
  help re-simulate any already-past day) — scoped as a distinct future
  effort, not part of this script.
- `Instrument.lot_size`/`tick_size` (`UNDERLYING_META` below) are real,
  user-confirmed values (2026-08-27: NIFTY 65, BANKNIFTY 30 — replacing the
  old illustrative `domain.market.mock_universe._UNDERLYINGS` test values,
  which were off by ~2.6x for NIFTY). BANKNIFTY has never been traded live
  by this system — backtest-only, no production `Instrument` row exists to
  cross-check against — so its figure rests on the user's own confirmation,
  not an independent re-derivation. `RiskLimitConfig`/`TradingSession`
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

Known pitfalls when doing a TARGETED rerun of only specific days from a
prior baseline (2026-08-26, `--dates`/`--pairs` below) -- three real,
non-obvious problems hit building this, each costing real wall-clock time
to diagnose because they only surface partway through a long run, not at
startup:

- **A day can genuinely fall inside more than one expiry directory's data
  range.** Real weekly-option contracts get listed weeks before their own
  expiry, so `_expiry_data_range()`'s true min/max for one expiry directory
  routinely reaches back 1-3 *other* expiry cycles' worth of calendar days.
  A naive "find the nearest expiry directory by name" guess for a given day
  (no data-range check) can pick a directory that is empty or simply
  doesn't cover that day at all — confirmed live: the nearest-by-name
  directory to 2025-08-19 was 2025-08-26, which had zero files, while the
  real data for that day lived in the 2025-09-02 directory (it starts
  2025-08-19, two weeks before its own expiry). `--dates` fixes this by
  reusing `--all-expiries`'s own already-correct range-checked iteration as
  a filter, never guessing.
- **That same overlap means an isolated single-day replay can "find" trades
  the original full sweep never actually recorded.** The original
  `--all-expiries` sweep keeps ONE continuous database/StrategyRun per
  expiry directory across that directory's *entire* multi-day range, and
  since a dispatched trade's `Position` never actually closes during replay
  (exits are reconstructed offline, afterward — see "No cross-trade P&L
  feedback" above), an earlier day's still-technically-open position can
  risk-reject (same-strike lock, concurrency) a later, overlapping day's
  signal *within that same continuous run*. Replaying each target day in
  total isolation (a fresh, empty database per day) removes that blocking
  state entirely, so a day covered by 3-4 overlapping expiry directories
  can independently "succeed" in every one of them, producing 2-3x more
  candidate trades than the original baseline ever recorded for that day.
  Confirmed live: 2025-09-23 and 2025-09-30 each produced 3 candidate
  trades in isolation vs. the 1 the original baseline actually recorded for
  each. Don't treat an isolated targeted rerun's own trade *count* as
  ground truth for which trades were real -- it isn't.
- **Seeding the same real expiry's option contracts twice in one process
  collides on real, global uniqueness constraints**
  (`uq_instrument_symbol_exchange`, `uq_option_contract_symbol` — the
  latter is on the actual broker symbol string, e.g.
  "NIFTY25093024350CE", which is NOT scoped to this script's own
  per-expiry `Instrument` isolation trick). This *will* happen the moment
  more than one requested day/pair maps to the same expiry directory
  within a single process (the norm once the overlap above is accounted
  for) unless the database is reset between calls — `--dates`/`--pairs`
  both call `Base.metadata.drop_all`/`create_all` before every single-day
  `_run_single_backtest` call for exactly this reason. Cheap per call (one
  day's worth of data), but real DDL overhead multiplies fast if a naive
  `--dates` filter processes every overlapping directory per day instead
  of the one that's actually real.

**The fix that sidesteps all three at once, when the exact answer is
already known**: a real option-contract symbol encodes its own real expiry
date (e.g. "NIFTY25093024350CE" -> 2025-09-30) — parsing that directly out
of a prior baseline CSV's own rows gives the EXACT (day, expiry) pair each
real trade actually came from, with zero ambiguity and zero redundant
overlap. `--pairs 'YYYY-MM-DD:YYYY-MM-DD,...'` takes that directly (day and
expiry directory both named explicitly, no scan, no guessing, no
post-filtering needed) — strictly preferred over `--dates` whenever a
baseline CSV to parse pairs from already exists; `--dates` is the fallback
for when it doesn't (e.g. targeting days that never had a prior baseline
run at all) and must therefore accept the overlap cost above.
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

from backtest_pivots import PivotLevels, compute_floor_pivots, prior_day_ohlc  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.api.v1.strategies import _DEFAULT_QTY_LOTS_PAPER, _build_strategy  # noqa: E402
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
from app.domain.execution.models import ExitReason  # noqa: E402
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
    SPREAD_BLOWOUT_PCT,
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

# Real, user-confirmed lot sizes (2026-08-27 fidelity fix — replacing the
# old `mock_universe._UNDERLYINGS` illustrative test values, which were off
# by ~2.6x for NIFTY). NIFTY's 65 is independently corroborated in-repo by
# the real Shoonya scrip-master parse (see `tests/unit/test_shoonya_scrip_
# master.py`). BANKNIFTY's 30 has no production `Instrument` row to
# cross-check against — it's never been traded live, backtest-only — so the
# user's own figure is the ground truth here, not something this script
# re-derives.
UNDERLYING_META: dict[str, tuple[int, float]] = {  # (lot_size, tick_size)
    "NIFTY": (65, 0.05),
    "BANKNIFTY": (30, 0.05),
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


def _liquidity_score(
    oi: int, volume: int, oi_lo: int, oi_hi: int, vol_lo: int, vol_hi: int
) -> float:
    """OI/volume-derived liquidity proxy, 0..1, min-max normalized across
    whatever cross-section the caller supplies (a single chain-snapshot
    cycle's live contracts for `HistoricalBrokerAdapter.get_option_chain`;
    every contract with a bar at-or-before a given exit-walk timestamp for
    `_spread_pct_at`) — same convention `strike_ranking.engine._normalize`
    already uses. Module-level (2026-08-27, extracted out of
    `get_option_chain`'s own closure) so both the entry-side chain and the
    exit-side spread-blowout check (`--exit-mode current`) share one
    formula, not two independently-maintained copies.
    """
    oi_n = 1.0 if oi_hi == oi_lo else (oi - oi_lo) / (oi_hi - oi_lo)
    vol_n = 1.0 if vol_hi == vol_lo else (volume - vol_lo) / (vol_hi - vol_lo)
    return 0.5 * oi_n + 0.5 * vol_n


def _synthetic_spread_pct(liquidity: float) -> float:
    """`liquidity` (0..1, from `_liquidity_score`) -> a synthetic spread_pct
    interpolated between `MAX_SYNTHETIC_SPREAD_PCT` (illiquid) and
    `MIN_SYNTHETIC_SPREAD_PCT` (liquid) — see module docstring's synthetic
    bid/ask/depth section for why this proxy exists at all (no historical
    source here carries real bid/ask/depth)."""
    spread_range = MAX_SYNTHETIC_SPREAD_PCT - MIN_SYNTHETIC_SPREAD_PCT
    return MAX_SYNTHETIC_SPREAD_PCT - liquidity * spread_range


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

        entries: list[OptionChainEntry] = []
        depth_by_symbol: dict[str, int] = {}
        total_ce_oi = 0
        total_pe_oi = 0
        for symbol, strike, option_type, bar in raw:
            liquidity = _liquidity_score(bar.oi, bar.volume, oi_lo, oi_hi, vol_lo, vol_hi)
            spread_pct = _synthetic_spread_pct(liquidity)
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
    # Which exit-mode leg this row belongs to (2026-08-24, see
    # backtest_pivots.py / --exit-mode) -- "legacy" for every row produced
    # by the original `_reconstruct_exit` (single fixed-%-target leg, the
    # only mode that existed before this field was added), or one of
    # "near_target"/"far_target"/"no_target" for `_reconstruct_exit_legs`
    # rows. A `split_30_30_40` entry produces up to 3 rows sharing the same
    # entry_time/entry_price/symbol -- sum their pnl for that entry's total.
    leg: str = "legacy"
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


def _lookup_nearest_minute(series: list[tuple[datetime, float]], ts: datetime) -> float | None:
    if not series:
        return None
    idx = bisect_right([s[0] for s in series], ts) - 1
    return series[idx][1] if idx >= 0 else None


class DiagnosticsSource:
    """VIX + PCR lookups for the diagnostic-only barometers on
    `ReconstructedTrade` -- see that class's own docstring. ATR is tracked
    separately (`ATRTracker`, a sequential stream, not point-lookup) since
    it needs the full bar history up to a point, not just one value at a
    timestamp.

    VIX source switched 2026-08-24 from TrueData (1-min only for the live
    ~12-day window, EOD-only beyond that) to Alice Blue
    (`fetch_alice_blue_underlying_history.py --underlyings INDIA_VIX`,
    confirmed live: real, gap-free 1-min candles back ~2.6 years) -- real
    1-min resolution across this whole script's actual backtest window now,
    so the old EOD fallback tier is gone, not just unused.
    """

    def __init__(self, data_dir: Path) -> None:
        self.vix_minute = _load_minute_series(
            data_dir / "underlyings" / "INDIA_VIX_alice_index_1min.csv"
        )

    def vix_at(self, ts: datetime) -> float | None:
        return _lookup_nearest_minute(self.vix_minute, ts)


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


def _spread_pct_at(
    contracts: list[tuple[str, float, ContractOptionType]],
    option_bars: dict[str, list[Bar]],
    ts: datetime,
    symbol: str,
) -> float | None:
    """`--exit-mode current`'s exit-time synthetic spread_pct for `symbol`
    — the exact same OI/volume-derived liquidity proxy
    `HistoricalBrokerAdapter.get_option_chain` uses at entry time
    (`_liquidity_score`/`_synthetic_spread_pct`), cross-sectionally
    normalized across every contract with a bar at-or-before `ts`, mirroring
    `_pcr_at`'s own pattern exactly. Not a literal replay of a real
    historical chain-snapshot moment — this backtest only ever snapshotted
    the chain at real entry-evaluation cycles during the original replay,
    not at every possible later exit-walk timestamp — but it's the same
    formula applied consistently, not a second, independently-invented
    approximation. Returns `None` if `symbol` has no bar at-or-before `ts`.
    """
    ois: list[int] = []
    volumes: list[int] = []
    target_bar: Bar | None = None
    for c_symbol, _strike, _option_type in contracts:
        bars = option_bars.get(c_symbol)
        if not bars:
            continue
        ts_list = [b.ts for b in bars]
        idx = bisect_right(ts_list, ts) - 1
        if idx < 0:
            continue
        bar = bars[idx]
        ois.append(bar.oi)
        volumes.append(bar.volume)
        if c_symbol == symbol:
            target_bar = bar
    if target_bar is None:
        return None
    oi_lo, oi_hi = (min(ois), max(ois)) if ois else (0, 0)
    vol_lo, vol_hi = (min(volumes), max(volumes)) if volumes else (0, 0)
    liquidity = _liquidity_score(target_bar.oi, target_bar.volume, oi_lo, oi_hi, vol_lo, vol_hi)
    return _synthetic_spread_pct(liquidity)


class _StructureBreakCandidate:
    """Local, in-memory mirror of production's `StopPlan.structure_break_
    candidate_since`/`_extreme` columns (`evaluate_open_position` step 3,
    `execution_engine/paper/service.py`) — scoped to one trade's own
    forward walk. This backtest's stray real `StopPlan` rows (created by
    the real `dispatch_trade_intent` every approved intent already goes
    through) are never read back by any reconstruction function — see
    module docstring — so mirroring the math locally, not round-tripping
    through the DB, is the correct and established pattern here (same as
    how stop/target/trail already work in `_reconstruct_exit`)."""

    __slots__ = ("since", "extreme")

    def __init__(self) -> None:
        self.since: datetime | None = None
        self.extreme: float | None = None


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
    target_price_override: Decimal | None = None,
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

    `target_price_override` (2026-08-26, default `None` -- every call site
    before this date, and every call site today that doesn't pass it, is
    completely unaffected): substitutes the trade's own fixed-%% target with
    a caller-supplied one (e.g. `--exit-mode target_mult`'s "same stop, N x
    the target distance" test) while leaving stop/trail untouched -- trail
    activation/lock still anchors to this same substituted value, exactly
    like it already anchors to the real target for the pivot-mode legs in
    `_reconstruct_exit_legs` below.
    """
    entry_time = trade_intent.created_at
    entry_price = Decimal(str(trade_intent.entry_price))
    stop_price = Decimal(str(trade_intent.stop_price))
    target_price = (
        target_price_override
        if target_price_override is not None
        else Decimal(str(trade_intent.target_price))
    )
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


def _reconstruct_exit_current(
    trade_intent: TradeIntent,
    symbol: str,
    option_bars: list[Bar],
    lot_size: int,
    qty_lots: int,
    *,
    underlying_series: list[tuple[datetime, float]],
    entry_diagnostics: dict[str, float | int | None] | None = None,
    diagnostics: DiagnosticsSource | None = None,
    all_contracts: list[tuple[str, float, ContractOptionType]] | None = None,
    all_option_bars: dict[str, list[Bar]] | None = None,
    atr_series: list[tuple[datetime, float]] | None = None,
) -> ReconstructedTrade:
    """`--exit-mode current` (2026-08-27) — the faithful default, replacing
    `legacy` as `main()`'s own CLI default while `legacy` itself stays
    completely untouched (frozen, byte-identical to every pre-2026-08-24
    run — see `_reconstruct_exit`'s own docstring; this is a deliberately
    SEPARATE function, not a refactor of it, for exactly the same
    reproducibility reason `_reconstruct_exit_legs` already gives for its
    own separateness).

    Implements all 5 real steps of `execution_engine.paper.service
    .evaluate_open_position` — stop, target, structure-break,
    spread-blowout, trail, in that exact order — using each bar's high/low,
    not just its close, since production checks live ticks continuously
    rather than once a minute. See module docstring's "Known, deliberate
    approximations" section for what's now modeled vs. what remains a
    genuine, permanent ceiling of the historical data available (no source
    offers real bid/ask or sub-minute bars).

    `qty_lots` is an explicit caller-supplied value, not
    `trade_intent.qty_lots` (which is always the pinned stub `1` regardless
    of exit-mode — set once at signal-generation time before any exit-mode
    branching runs, see `strategy_config_stub`'s own comment). The caller
    passes `app.api.v1.strategies._DEFAULT_QTY_LOTS_PAPER` (today's real
    paper default), so this mode's PnL is real-scale without any post-hoc
    rescaling — the same established pattern `_reconstruct_exit_legs`'s own
    `LegSpec.qty_lots` already uses for the identical reason.

    Same-bar tie-break (confirmed with the user, 2026-08-27 fidelity plan):
    a single bar's [low, high] range can plausibly satisfy more than one
    condition at 1-min granularity — a real, common occurrence here even
    though it's near-impossible on a live tick. Resolved by applying
    production's own fixed check order (the loop below) and taking the
    first condition, in that order, whose broadened intrabar test is true —
    conservative/loss-first, matching both production's own stated
    defense-in-depth reasoning (`evaluate_open_position`'s own docstring)
    and this file's established "never read more optimistic than a real
    run would" philosophy.

    Known, permanent limitations kept honest here rather than glossed over:
    the structure-break persistence timer (every strategy's real default is
    6.0s, `common_rules.DEFAULT_STRUCTURE_BREAK_PERSISTENCE_SECONDS`)
    always collapses to "survives to the next completed 1-min bar" at this
    granularity, not production's real "survives ~2 live 3s poll cycles" —
    unavoidable at 1-min resolution. Spread-blowout still relies on the
    same synthetic OI/volume-derived proxy used everywhere else in this
    script (no historical source — TrueData, Shoonya TPSeries, Alice
    Blue — offers real bid/ask/depth at any granularity). The trail's
    same-bar tightening can't distinguish a genuine intra-minute
    run-up-then-reversal from a last-second spike-then-reversal — both
    produce identical OHLC.
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

    structure_level = (
        Decimal(str(trade_intent.structure_level))
        if trade_intent.structure_level is not None
        else None
    )
    structure_buffer = Decimal(str(trade_intent.structure_break_buffer or 0))
    structure_persistence = float(trade_intent.structure_break_persistence_seconds or 0)
    candidate = _StructureBreakCandidate()

    entry_diagnostics = entry_diagnostics or {}
    base = ReconstructedTrade(
        symbol=symbol,
        side=side.value,
        entry_time=entry_time,
        entry_price=float(entry_price),
        exit_time=None,
        exit_price=None,
        exit_reason="no_further_data",
        qty_lots=qty_lots,
        lot_size=lot_size,
        leg="current",
        vix_entry=entry_diagnostics.get("vix"),  # type: ignore[arg-type]
        atr_entry=entry_diagnostics.get("atr"),  # type: ignore[arg-type]
        pcr_entry=entry_diagnostics.get("pcr"),  # type: ignore[arg-type]
        contract_oi_entry=entry_diagnostics.get("contract_oi"),  # type: ignore[arg-type]
    )

    def _exit(ts: datetime, price: Decimal, reason: str) -> ReconstructedTrade:
        base.exit_time, base.exit_price, base.exit_reason = ts, float(price), str(reason)
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
        close = Decimal(str(bar.close))
        high = Decimal(str(bar.high))
        low = Decimal(str(bar.low))

        if bar.ts.time() >= EOD_CUTOFF:
            return _exit(bar.ts, close, ExitReason.EOD_SQUARE_OFF)

        # Step 1: stop — intrabar (was close-only).
        hit_stop = low <= stop_price if favorable else high >= stop_price
        if hit_stop:
            return _exit(bar.ts, stop_price, ExitReason.STOP)

        # Step 2: target — intrabar (was close-only).
        hit_target = high >= target_price if favorable else low <= target_price
        if hit_target:
            return _exit(bar.ts, target_price, ExitReason.TARGET)

        # Step 3: structure-break (not modeled at all before this function).
        if structure_level is not None:
            underlying_price = _lookup_nearest_minute(underlying_series, bar.ts)
            if underlying_price is not None:
                underlying = Decimal(str(underlying_price))
                buffered_level = (
                    structure_level - structure_buffer
                    if favorable
                    else structure_level + structure_buffer
                )
                breached = (
                    underlying < buffered_level if favorable else underlying > buffered_level
                )
                if breached:
                    if candidate.since is None:
                        candidate.since = bar.ts
                        candidate.extreme = underlying_price
                    else:
                        assert candidate.extreme is not None
                        worse = (
                            underlying_price < candidate.extreme
                            if favorable
                            else underlying_price > candidate.extreme
                        )
                        if worse:
                            candidate.extreme = underlying_price
                    elapsed = (bar.ts - candidate.since).total_seconds()
                    if elapsed >= structure_persistence:
                        # Bar-close confirmation (production's own
                        # `_structure_break_confirmed_by_bar_close`):
                        # `underlying_price` above is already the latest
                        # *completed* underlying bar's own close as of this
                        # option bar's own evaluation moment (both are on
                        # the same 1-min grid, and this whole walk already
                        # treats each bar's close as "current price as of
                        # that bar" — same convention `_reconstruct_exit`
                        # and the pivot-leg underlying lookup already use).
                        # So a breach that reaches this point has, by
                        # construction, already closed beyond the buffered
                        # level — no separate query needed. When
                        # `structure_persistence == 0` (strategy hasn't
                        # opted in), this reaches here on the very first
                        # breaching bar, matching production's own instant-
                        # confirm fallback exactly.
                        return _exit(bar.ts, close, ExitReason.STRUCTURE_BREAK)
                elif candidate.since is not None:
                    candidate.since = None
                    candidate.extreme = None

        # Step 4: spread-blowout (not modeled at all before this function).
        if all_contracts is not None and all_option_bars is not None:
            spread_pct = _spread_pct_at(all_contracts, all_option_bars, bar.ts, symbol)
            if spread_pct is not None and Decimal(str(spread_pct)) > SPREAD_BLOWOUT_PCT:
                return _exit(bar.ts, close, ExitReason.SPREAD_BLOWOUT)

        # Step 5: trail — intrabar tightening using this bar's own favorable
        # extreme, then testing its unfavorable extreme for a same-bar hit.
        # A per-bar lumped approximation — see this function's own
        # docstring for the exact limitation.
        favorable_extreme = high if favorable else low
        unfavorable_extreme = low if favorable else high
        activated = (
            favorable_extreme >= activation_price
            if favorable
            else favorable_extreme <= activation_price
        )
        if activated:
            gain_beyond = (
                (favorable_extreme - activation_price)
                if favorable
                else (activation_price - favorable_extreme)
            )
            locked_gain = gain_beyond * lock_fraction
            new_trail_stop = (
                activation_price + locked_gain if favorable else activation_price - locked_gain
            )
            if trail_stop is None or (
                new_trail_stop > trail_stop if favorable else new_trail_stop < trail_stop
            ):
                trail_stop = new_trail_stop
            hit_trail = (
                unfavorable_extreme < trail_stop
                if favorable
                else unfavorable_extreme > trail_stop
            )
            if hit_trail:
                return _exit(bar.ts, trail_stop, ExitReason.TRAIL)

    return base


# ---------------------------------------------------------------------------
# Pivot-anchored split-leg exit reconstruction (2026-08-24)
#
# Generalizes `_reconstruct_exit` above to walk `option_bars` once while
# evaluating N independently-managed "legs" of the same entry instead of
# one -- each leg shares the same entry/stop, but can have its own target
# rule: a classic-floor-pivot level on the *underlying* (near = R1/S1, far
# = R2/S2 -- see backtest_pivots.py) or no target at all (a pure
# trail-only runner). See project plan / memory for the full design
# writeup ("Pivot-level split-leg exits for backtesting").
#
# Deliberately a SEPARATE function from `_reconstruct_exit`, not a
# refactor of it -- `_reconstruct_exit` stays byte-for-byte unchanged so
# `--exit-mode legacy` (the default) keeps reproducing every baseline CSV
# already gathered this week, not just "should be equivalent."
# ---------------------------------------------------------------------------

EXIT_MODES = (
    "legacy", "current", "near_only", "far_only", "no_target_only", "split_30_30_40",
    "target_mult",
)


@dataclass
class LegSpec:
    label: str  # "near_target" | "far_target" | "no_target"
    qty_lots: int
    target_mode: str  # "pivot" | "none"
    pivot_level: float | None = None  # underlying index level, only for target_mode="pivot"


def _resolve_leg_specs(
    exit_mode: str,
    total_lots: int,
    option_type: DomainOptionType,
    pivots: PivotLevels | None,
    underlying_spot_at_entry: float | None,
) -> tuple[list[LegSpec], bool]:
    """Turns `--exit-mode`/`--total-lots` into concrete `LegSpec`s for one
    trade intent. Returns `(leg_specs, pivot_direction_up)` -- the latter
    is shared across every leg of the same entry (CE/bullish wants the
    underlying to rise toward resistance; PE/bearish wants it to fall
    toward support), not a per-leg property.

    A pivot level that isn't actually ahead of the underlying's own spot
    price at entry time (can happen -- pivots are *prior*-day, price can
    gap past them intraday) silently degrades that one leg to `no_target`
    for this entry only, rather than exit instantly or crash -- logged once
    per occurrence so it's visible in the run's own console output without
    aborting anything.
    """
    is_ce = option_type == DomainOptionType.CE
    pivot_direction_up = is_ce

    def _level(*, near: bool) -> float | None:
        if pivots is None or underlying_spot_at_entry is None:
            return None
        level = (pivots.r1 if near else pivots.r2) if is_ce else (pivots.s1 if near else pivots.s2)
        ahead = level > underlying_spot_at_entry if is_ce else level < underlying_spot_at_entry
        return level if ahead else None

    near_level = _level(near=True)
    far_level = _level(near=False)

    def _leg(label: str, lots: int) -> LegSpec:
        level = near_level if label == "near_target" else far_level
        if level is None:
            print(
                f"  [pivot-fallback] {label} level not ahead of entry spot "
                "-- degrading this leg to no_target for this entry"
            )
            return LegSpec("no_target", lots, "none")
        return LegSpec(label, lots, "pivot", pivot_level=level)

    if exit_mode == "near_only":
        return [_leg("near_target", total_lots)], pivot_direction_up
    if exit_mode == "far_only":
        return [_leg("far_target", total_lots)], pivot_direction_up
    if exit_mode == "no_target_only":
        return [LegSpec("no_target", total_lots, "none")], pivot_direction_up
    if exit_mode == "split_30_30_40":
        near_lots = round(total_lots * 0.3)
        far_lots = round(total_lots * 0.3)
        runner_lots = total_lots - near_lots - far_lots
        specs = [
            _leg("near_target", near_lots),
            _leg("far_target", far_lots),
            LegSpec("no_target", runner_lots, "none"),
        ]
        return [s for s in specs if s.qty_lots > 0], pivot_direction_up
    raise ValueError(f"unknown exit_mode for leg splitting: {exit_mode}")


def _reconstruct_exit_legs(
    trade_intent: TradeIntent,
    symbol: str,
    option_bars: list[Bar],
    lot_size: int,
    leg_specs: list[LegSpec],
    *,
    underlying_series: list[tuple[datetime, float]],
    pivot_direction_up: bool,
    entry_diagnostics: dict[str, float | int | None] | None = None,
    diagnostics: DiagnosticsSource | None = None,
    all_contracts: list[tuple[str, float, ContractOptionType]] | None = None,
    all_option_bars: dict[str, list[Bar]] | None = None,
    atr_series: list[tuple[datetime, float]] | None = None,
) -> list[ReconstructedTrade]:
    """Same steps 1 (stop), 2 (target), 5 (trail) priority order as
    `_reconstruct_exit`, evaluated once per bar for every leg in
    `leg_specs` instead of a single target/qty. Step 2 differs per leg:
    `target_mode="pivot"` exits when the *underlying's* price (looked up
    via `underlying_series`, same `_lookup_nearest_minute` pattern already
    used for VIX/ATR) crosses `pivot_level` favorably, exiting at the
    option bar's own current premium (mark-to-market -- there's no
    reliable index-point-distance -> premium-distance conversion without
    live delta/greeks data this system doesn't have, so this deliberately
    mirrors how `structure_level`'s own stop check already works: compare
    on the underlying, execute at the option's current price).
    `target_mode="none"` never exits on target at all.

    Trail activation/lock is anchored to `trade_intent.target_price` (the
    strategy's own original fixed-% target) purely as a distance yardstick
    for every leg, including pivot-target ones -- unchanged from
    `_reconstruct_exit`, not each leg's own pivot level, per the same "no
    reliable premium-distance conversion" reasoning above.
    """
    entry_time = trade_intent.created_at
    entry_price = Decimal(str(trade_intent.entry_price))
    stop_price = Decimal(str(trade_intent.stop_price))
    side = SignalSide(trade_intent.side)
    favorable = side == SignalSide.BUY

    legacy_target = Decimal(str(trade_intent.target_price))
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
    activation_distance = abs(legacy_target - entry_price) * activation_fraction
    activation_price = (
        entry_price + activation_distance if favorable else entry_price - activation_distance
    )

    entry_diagnostics = entry_diagnostics or {}

    class _LegState:
        __slots__ = ("spec", "trail_stop", "result")

        def __init__(self, spec: LegSpec) -> None:
            self.spec = spec
            self.trail_stop: Decimal | None = None
            self.result: ReconstructedTrade | None = None

    states = [_LegState(spec) for spec in leg_specs]

    def _finalize(state: _LegState, ts: datetime, price: Decimal, reason: str) -> None:
        trade = ReconstructedTrade(
            symbol=symbol,
            side=side.value,
            entry_time=entry_time,
            entry_price=float(entry_price),
            exit_time=ts,
            exit_price=float(price),
            exit_reason=reason,
            qty_lots=state.spec.qty_lots,
            lot_size=lot_size,
            leg=state.spec.label,
            vix_entry=entry_diagnostics.get("vix"),  # type: ignore[arg-type]
            atr_entry=entry_diagnostics.get("atr"),  # type: ignore[arg-type]
            pcr_entry=entry_diagnostics.get("pcr"),  # type: ignore[arg-type]
            contract_oi_entry=entry_diagnostics.get("contract_oi"),  # type: ignore[arg-type]
        )
        if diagnostics is not None:
            trade.vix_exit = diagnostics.vix_at(ts)
        if all_contracts is not None and all_option_bars is not None:
            trade.pcr_exit = _pcr_at(all_contracts, all_option_bars, ts)
        if atr_series is not None:
            trade.atr_exit = _lookup_nearest_minute(atr_series, ts)
        trade.contract_oi_exit = _contract_oi_at(option_bars, ts)
        state.result = _with_pnl(trade, side)

    for bar in option_bars:
        if bar.ts < entry_time:
            continue
        if all(s.result is not None for s in states):
            break
        price = Decimal(str(bar.close))

        if bar.ts.time() >= EOD_CUTOFF:
            for s in states:
                if s.result is None:
                    _finalize(s, bar.ts, price, "eod_square_off")
            break

        underlying_price = _lookup_nearest_minute(underlying_series, bar.ts)

        for s in states:
            if s.result is not None:
                continue

            hit_stop = price <= stop_price if favorable else price >= stop_price
            if hit_stop:
                _finalize(s, bar.ts, stop_price, "stop")
                continue

            if (
                s.spec.target_mode == "pivot"
                and s.spec.pivot_level is not None
                and underlying_price is not None
            ):
                level = s.spec.pivot_level
                hit_target = (
                    underlying_price >= level if pivot_direction_up else underlying_price <= level
                )
                if hit_target:
                    _finalize(s, bar.ts, price, "target")
                    continue

            activated = price >= activation_price if favorable else price <= activation_price
            if activated:
                gain_beyond = (
                    (price - activation_price) if favorable else (activation_price - price)
                )
                locked_gain = gain_beyond * lock_fraction
                new_trail_stop = (
                    activation_price + locked_gain
                    if favorable
                    else activation_price - locked_gain
                )
                if s.trail_stop is None or (
                    new_trail_stop > s.trail_stop if favorable else new_trail_stop < s.trail_stop
                ):
                    s.trail_stop = new_trail_stop
                hit_trail = price < s.trail_stop if favorable else price > s.trail_stop
                if hit_trail:
                    _finalize(s, bar.ts, s.trail_stop, "trail")

    results: list[ReconstructedTrade] = []
    for s in states:
        if s.result is not None:
            results.append(s.result)
            continue
        results.append(
            ReconstructedTrade(
                symbol=symbol,
                side=side.value,
                entry_time=entry_time,
                entry_price=float(entry_price),
                exit_time=None,
                exit_price=None,
                exit_reason="no_further_data",
                qty_lots=s.spec.qty_lots,
                lot_size=lot_size,
                leg=s.spec.label,
                vix_entry=entry_diagnostics.get("vix"),  # type: ignore[arg-type]
                atr_entry=entry_diagnostics.get("atr"),  # type: ignore[arg-type]
                pcr_entry=entry_diagnostics.get("pcr"),  # type: ignore[arg-type]
                contract_oi_entry=entry_diagnostics.get("contract_oi"),  # type: ignore[arg-type]
            )
        )
    return results


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

    legs = {t.leg for t in resolved}
    if legs != {"legacy"}:
        print("\nPer-leg breakdown:")
        for leg in sorted(legs):
            leg_trades = [t for t in resolved if t.leg == leg]
            leg_wins = [t for t in leg_trades if t.pnl and t.pnl > 0]
            leg_pnl = sum(t.pnl for t in leg_trades if t.pnl is not None)
            leg_win_rate = (len(leg_wins) / len(leg_trades) * 100) if leg_trades else 0.0
            print(
                f"  {leg:<12} {len(leg_trades):>4} trade(s)  "
                f"win rate {leg_win_rate:5.1f}%  total pnl {leg_pnl:+.2f}"
            )

        # Per-entry (entry_time, symbol) summed PnL -- the number directly
        # comparable to a `*_only` mode's own total PnL above, since a
        # split entry's "real" result is the sum of its legs, not any one
        # leg read in isolation.
        by_entry: dict[tuple[datetime, str], float] = {}
        for t in resolved:
            key = (t.entry_time, t.symbol)
            by_entry[key] = by_entry.get(key, 0.0) + (t.pnl or 0.0)
        consolidated_pnl = sum(by_entry.values())
        print(
            f"\nConsolidated (per-entry summed) total PnL across all legs: "
            f"{consolidated_pnl:+.2f} ({len(by_entry)} entries)"
        )


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
                "symbol", "side", "leg", "entry_time", "entry_price", "exit_time", "exit_price",
                "exit_reason", "qty_lots", "lot_size", "pnl",
                "vix_entry", "vix_exit", "atr_entry", "atr_exit",
                "pcr_entry", "pcr_exit", "contract_oi_entry", "contract_oi_exit",
            ]
        )
        for t in sorted(trades, key=lambda x: x.entry_time):
            writer.writerow(
                [
                    t.symbol, t.side, t.leg, to_ist(t.entry_time).isoformat(), t.entry_price,
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
    "symbol", "side", "leg", "entry_time", "entry_price", "exit_time", "exit_price",
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
                    t.symbol, t.side, t.leg, to_ist(t.entry_time).isoformat(), t.entry_price,
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
    exit_modes: list[str] | None = None,
    total_lots: int = 10,
    underlying_series: list[tuple[datetime, float]] | None = None,
    target_multiplier: float = 2.0,
) -> tuple[dict[str, list[ReconstructedTrade]], int, Counter[str], int]:
    """One expiry's full seed -> replay -> risk-outcome -> exit-reconstruction
    pass — the exact single-run body `main()` used to run inline, now
    reusable so `--all-expiries` can call it once per discovered expiry
    directory and aggregate. Returns
    (trades_by_exit_mode, risk_rejected_count, risk_rejected_reasons, total_signals).

    `exit_modes` (2026-08-24, defaults to `["legacy"]` -- see `EXIT_MODES`):
    the expensive part of this function is the bar-by-bar replay through the
    real strategy/risk pipeline above, which produces an identical set of
    approved `TradeIntent`s regardless of exit-mode -- exit-mode only
    affects the final reconstruction step. Passing more than one mode here
    reuses that single replay pass and reconstructs it N ways instead of
    requiring N full separate invocations (each redoing the same expensive
    replay for a difference that only shows up in the last few lines) --
    the reason `--exit-mode all` exists on `main()`'s own CLI flag.

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
    modes = exit_modes if exit_modes is not None else ["legacy"]

    warmup_bars = [b for b in all_underlying_bars if b.ts.date() < from_date][-1000:]
    main_bars = [b for b in all_underlying_bars if from_date <= b.ts.date() <= to_date]
    if not main_bars:
        if not quiet:
            print(f"  [{expiry_date.isoformat()}] no underlying bars in window, skipping")
        return {m: [] for m in modes}, 0, Counter(), 0

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
        # Pinned explicitly, not left to `_build_strategy`'s own mode-aware
        # default (2026-08-24, see that function's docstring) -- this stub
        # is never flushed to a session, so its unset `status`/`runtime_mode`
        # would resolve as "paper" (default 10) rather than the `1` every
        # baseline CSV gathered before this date was built against. This
        # script's own `--total-lots` (default 10) is the deliberate,
        # independent lever for exit-mode reconstruction sizing instead --
        # see EXIT_MODES / `_resolve_leg_specs`.
        params={"qty_lots": 1},
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
    option_type_by_intent: dict[uuid.UUID, DomainOptionType] = {}
    pivots_by_date: dict[date, PivotLevels | None] = {}

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
                        option_type_by_intent[trade_intent.id] = option_contract.option_type
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
    trades_by_mode: dict[str, list[ReconstructedTrade]] = {m: [] for m in modes}
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

            for mode in modes:
                if mode == "legacy":
                    trades_by_mode[mode].append(
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
                    continue

                if mode == "current":
                    assert underlying_series is not None  # guaranteed by main(), every call site
                    trades_by_mode[mode].append(
                        _reconstruct_exit_current(
                            trade_intent,
                            symbol,
                            bars,
                            ctx.lot_size,
                            _DEFAULT_QTY_LOTS_PAPER,
                            underlying_series=underlying_series,
                            entry_diagnostics=entry_diagnostics_by_intent.get(intent_id),
                            diagnostics=diagnostics,
                            all_contracts=contracts,
                            all_option_bars=option_bars,
                            atr_series=atr_series,
                        )
                    )
                    continue

                if mode == "target_mult":
                    entry_price = Decimal(str(trade_intent.entry_price))
                    orig_target = Decimal(str(trade_intent.target_price))
                    favorable = SignalSide(trade_intent.side) == SignalSide.BUY
                    distance = abs(orig_target - entry_price) * Decimal(str(target_multiplier))
                    new_target = entry_price + distance if favorable else entry_price - distance
                    mult_trade = _reconstruct_exit(
                        trade_intent,
                        symbol,
                        bars,
                        ctx.lot_size,
                        entry_diagnostics=entry_diagnostics_by_intent.get(intent_id),
                        diagnostics=diagnostics,
                        all_contracts=contracts,
                        all_option_bars=option_bars,
                        atr_series=atr_series,
                        target_price_override=new_target,
                    )
                    mult_trade.leg = "target_mult"
                    trades_by_mode[mode].append(mult_trade)
                    continue

                assert underlying_series is not None  # guaranteed by main() for non-legacy modes
                entry_date = trade_intent.created_at.date()
                if entry_date not in pivots_by_date:
                    ohlc = prior_day_ohlc(all_underlying_bars, entry_date)
                    pivots_by_date[entry_date] = (
                        compute_floor_pivots(*ohlc) if ohlc is not None else None
                    )
                pivots = pivots_by_date[entry_date]
                underlying_spot_at_entry = _lookup_nearest_minute(
                    underlying_series, trade_intent.created_at
                )
                leg_specs, pivot_direction_up = _resolve_leg_specs(
                    mode,
                    total_lots,
                    option_type_by_intent[intent_id],
                    pivots,
                    underlying_spot_at_entry,
                )
                trades_by_mode[mode].extend(
                    _reconstruct_exit_legs(
                        trade_intent,
                        symbol,
                        bars,
                        ctx.lot_size,
                        leg_specs,
                        underlying_series=underlying_series,
                        pivot_direction_up=pivot_direction_up,
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
    return trades_by_mode, risk_rejected_count, risk_rejected_reasons, total_signals


def _current_mode_label(strategy_type: str) -> str:
    """Bracketed label for `--exit-mode current`'s report header, e.g.
    `current [stop 12% / target 20%]` — read live off the actual `Strategy`
    subclass's own constructor defaults via the exact same `_build_strategy`
    mapping `_run_single_backtest` itself uses to build `strategy_obj`, not
    a second, hardcoded copy of these percentages. (2026-08-27, explicit
    user request: since 'current' always means "whatever production's real
    stop%/target% is today," and those constants can change later, the
    report must say what they actually were at the time, not just print
    the bare mode name — this function is what keeps that automatic rather
    than something to remember to update by hand.)

    Instrument/expiry/workspace ids are throwaway — `_build_strategy` only
    reads `strategy_config.params`/`.strategy_type`, matching the same
    minimal, never-flushed-to-a-session stub shape `_run_single_backtest`'s
    own real `strategy_config_stub` already uses.
    """
    stub = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        name="label-only",
        strategy_type=strategy_type,
        params={},
    )
    strategy_obj = _build_strategy(stub, uuid.uuid4(), date.today())
    stop_pct = getattr(strategy_obj, "stop_pct", None)
    target_pct = getattr(strategy_obj, "target_pct", None)
    if stop_pct is None or target_pct is None:
        return "current"
    return f"current [stop {stop_pct:.0%} / target {target_pct:.0%}]"


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
        "--underlying-source", choices=("spot", "futures_proxy", "alice_index"),
        default="alice_index",
        help="'alice_index' (default) = underlyings/<u>_alice_index_1min.csv (real, "
        "continuous NSE-index 1-min history via Alice Blue's historical chart API, "
        "~2.6 years with zero gaps -- confirmed 2026-08-24, see "
        "fetch_alice_blue_underlying_history.py); "
        "'spot' = underlyings/<u>_1min.csv (TrueData, ~12-day cap); "
        "'futures_proxy' = underlyings/<u>_underlying_proxy_1min.csv "
        "(stitched real monthly-futures history, real data only ~1wk/month near each "
        "contract's own expiry -- see fetch_truedata_futures_underlying_history.py). "
        "'spot'/'futures_proxy' files were deleted in the 2026-08-24 data cleanup as "
        "redundant with alice_index -- re-run fetch_truedata_historical.py/"
        "fetch_truedata_futures_underlying_history.py first if either is needed again.",
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
    parser.add_argument(
        "--exit-mode", default="current",
        help="Comma-separated list of exit modes, or 'all' for every mode. Choices: "
        f"{', '.join(EXIT_MODES)}. 'current' (default, 2026-08-27) = the faithful mode: all 5 "
        "real steps of execution_engine.paper.service.evaluate_open_position (stop, target, "
        "structure-break, spread-blowout, trail, in that exact order), using each bar's "
        "high/low (not just close), real qty_lots (api.v1.strategies._DEFAULT_QTY_LOTS_PAPER) "
        "and real lot sizes (UNDERLYING_META). Always means 'whatever production's real "
        "stop%%/target%% is today' -- see the printed report's own bracketed label (e.g. "
        "'current [stop 12%% / target 20%%]', from _current_mode_label) for the exact values in "
        "effect for this run, since those can change later without this mode's name changing. "
        "'legacy' = today's fixed-%%-target/stop/trail, close-only pricing, no structure-break/"
        "spread-blowout, pinned qty_lots=1 -- frozen, byte-identical to every pre-2026-08-24 "
        "run (and every run before this date), kept exactly as-is for reproducing old baseline "
        "CSVs; not the default any more. 'near_only'/'far_only' = 100%% of --total-lots exits "
        "at the underlying's R1/S1 or R2/S2 classic floor-pivot level (see backtest_pivots.py), "
        "computed off the prior trading day's OHLC. 'no_target_only' = 100%% of --total-lots, "
        "no target at all, stop/trail/EOD only. 'split_30_30_40' = one entry split "
        "30%%/30%%/40%% across near/far/no-target legs simultaneously (rounded to whole lots). "
        "'target_mult' = same entry/stop as legacy, target distance from entry scaled by "
        "--target-multiplier instead of the strategy's own fixed %%. near_only/far_only/"
        "no_target_only/split_30_30_40/target_mult are all deliberate what-if target-mechanism "
        "substitutions -- they still use legacy's own close-only/no-structure-break/"
        "no-spread-blowout mechanics via _reconstruct_exit/_reconstruct_exit_legs (untouched by "
        "this fidelity change), only their *target* logic deliberately deviates from real "
        "production. Multiple modes (comma-separated) or 'all' reuse a single replay pass (see "
        "_run_single_backtest's own docstring for why this is far cheaper than N separate "
        "invocations), writing one <out-csv-stem>_<mode>.csv per mode and printing one report "
        "per mode.",
    )
    parser.add_argument(
        "--total-lots", type=int, default=10,
        help="Total lot size the reconstruction scales to for every --exit-mode other than "
        "'legacy'/'target_mult' (which always use the real risk-approved TradeIntent's own "
        "qty_lots, for baseline reproducibility). Independent of production qty_lots defaults "
        "-- see backtest_pivots.py / project plan for why. Default 10, per the '10 lot test' "
        "this feature was built for.",
    )
    parser.add_argument(
        "--target-multiplier", type=float, default=2.0,
        help="Only used by --exit-mode target_mult: the target's distance from entry becomes "
        "this multiple of the strategy's own fixed-%% target distance (same direction, same "
        "stop price, same trail math anchored to the scaled target). Default 2.0.",
    )
    parser.add_argument(
        "--pairs", default=None,
        help="Comma-separated list of exact 'YYYY-MM-DD:YYYY-MM-DD' day:expiry pairs -- one "
        "single-day _run_single_backtest call per pair, with the expiry directory given "
        "directly (no --all-expiries scan, no nearest-expiry guess, no overlapping-directory "
        "redundancy). For rerunning EXACTLY the (day, expiry) combinations a prior baseline "
        "CSV's own rows already prove were real (parse each row's option symbol for its real "
        "expiry date) -- the tightest, fastest targeted rerun when the exact answer is already "
        "known, unlike --dates (which must try every directory whose data range could plausibly "
        "cover a day, since a day can genuinely fall inside more than one). Mutually exclusive "
        "with --all-expiries/--dates. Supports --shard-count/--shard-index (sharding the pair "
        "list).",
    )
    parser.add_argument(
        "--dates", default=None,
        help="Comma-separated list of ISO dates (YYYY-MM-DD). Requires --all-expiries: every "
        "expiry directory is still discovered and range-checked exactly as usual (a date can "
        "legitimately fall inside more than one expiry's own listed-contract window, and each "
        "one that does must still be replayed to reproduce every trade that date originally "
        "produced), but instead of one _run_single_backtest call covering an expiry's *entire* "
        "data range, one call is made per requested date that actually falls inside that "
        "range -- every other day in the range is skipped. For rerunning only the specific "
        "days a prior run's trades landed on, much cheaper than a full --all-expiries replay "
        "when most days in each expiry's window never produced a trade. NOTE: a plain "
        "nearest-expiry-by-name guess (no data-range check) is NOT equivalent -- an expiry "
        "directory can be empty or have a range that doesn't include a nearby date at all, "
        "which is exactly why this filters --all-expiries's own already-correct iteration "
        "instead of re-implementing expiry discovery.",
    )
    parser.add_argument(
        "--shard-count", type=int, default=1,
        help="Split the --all-expiries expiry list into this many shards for parallel "
        "invocations (round-robin by index, not contiguous ranges, so shards stay "
        "balanced even if some expiries have no data). Each shard MUST be given its own "
        "--db-suffix and --out-csv by the caller -- this script doesn't coordinate that "
        "itself. Default 1 (no sharding, identical to every prior run).",
    )
    parser.add_argument(
        "--shard-index", type=int, default=0,
        help="Which shard (0-indexed, < --shard-count) this invocation processes.",
    )
    args = parser.parse_args()
    if args.shard_count < 1 or not (0 <= args.shard_index < args.shard_count):
        raise SystemExit("--shard-index must be in [0, --shard-count)")
    if args.dates and not args.all_expiries:
        raise SystemExit("--dates requires --all-expiries (it filters which days within each "
                          "discovered expiry's own data range actually get replayed)")
    if args.pairs and (args.all_expiries or args.dates):
        raise SystemExit("--pairs is mutually exclusive with --all-expiries/--dates (it already "
                          "names the exact expiry directory per day, no scan needed)")
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

    # Built once here, not per-expiry inside `_run_single_backtest` -- same
    # "load once, thread through" pattern `diagnostics` below already uses.
    # Only actually needed for pivot-mode exit reconstruction (see
    # `_reconstruct_exit_legs`'s underlying-price lookups); harmless to
    # build unconditionally, it's a cheap O(n) pass over already-loaded bars.
    underlying_series: list[tuple[datetime, float]] = [
        (b.ts, b.close) for b in all_underlying_bars
    ]

    diagnostics = DiagnosticsSource(data_dir)

    _ensure_backtest_database_exists(db_suffix)
    engine = create_engine(_backtest_database_url(db_suffix), future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    if args.exit_mode == "all":
        exit_modes = list(EXIT_MODES)
    else:
        exit_modes = [m.strip() for m in args.exit_mode.split(",") if m.strip()]
        invalid_modes = [m for m in exit_modes if m not in EXIT_MODES]
        if invalid_modes:
            raise SystemExit(
                f"Invalid --exit-mode value(s) {invalid_modes!r}; choices are "
                f"{list(EXIT_MODES)!r} or 'all'"
            )
    multi_mode = len(exit_modes) > 1

    out_csv_base = args.out_csv or (
        REPO_ROOT / "data" / "historical" / "backtest_reports"
        / f"{args.strategy}_{args.underlying}_trades.csv"
    )

    def _out_csv_for(mode: str) -> Path:
        # Single-mode runs keep the exact original filename (backward
        # compatible with every script/analysis already pointed at it);
        # multi-mode ('--exit-mode all') runs get one file per mode instead
        # of clobbering each other.
        if not multi_mode:
            return out_csv_base
        return out_csv_base.with_name(f"{out_csv_base.stem}_{mode}{out_csv_base.suffix}")

    all_trades_by_mode: dict[str, list[ReconstructedTrade]] = {m: [] for m in exit_modes}
    total_risk_rejected = 0
    total_risk_rejected_reasons: Counter[str] = Counter()
    total_signals_all = 0

    if args.all_expiries:
        options_base = data_dir / args.options_subdir / args.underlying
        if not options_base.is_dir():
            raise SystemExit(f"Missing option data directory: {options_base}")
        expiry_dirs = sorted(
            (d for d in options_base.iterdir() if d.is_dir()), key=lambda d: d.name
        )
        if args.shard_count > 1:
            # 2026-08-24: the per-bar replay loop is Postgres-round-trip-
            # bound (one commit per bar, even with --fast), not CPU-bound --
            # each expiry is fully independent (its own seeded StrategyRun,
            # own contracts) so N parallel processes, each on its OWN
            # --db-suffix (a separate database -- no cross-shard locking at
            # all) and its own --out-csv, scale close to linearly up to
            # whatever the local Postgres/Docker setup can sustain. Caller
            # is responsible for merging each shard's per-mode CSVs
            # afterward (simple concatenation, header-once) -- this script
            # only filters which expiries *this* invocation processes.
            expiry_dirs = expiry_dirs[args.shard_index :: args.shard_count]
            print(
                f"Shard {args.shard_index}/{args.shard_count}: "
                f"{len(expiry_dirs)} of the full expiry list assigned to this process."
            )
        requested_dates: set[date] | None = None
        if args.dates:
            requested_dates = {
                date.fromisoformat(s.strip()) for s in args.dates.split(",") if s.strip()
            }
            print(
                f"--dates filter active: {len(requested_dates)} requested day(s), only these "
                "will actually be replayed within each expiry's own data range below."
            )
        print(f"Replaying {len(expiry_dirs)} expiries for {args.strategy}/{args.underlying} ...")
        wrote_header_by_mode = {m: False for m in exit_modes}
        for expiry_dir in expiry_dirs:
            expiry_date = date.fromisoformat(expiry_dir.name)
            date_range = _expiry_data_range(expiry_dir)
            if date_range is None:
                print(f"  [{expiry_date.isoformat()}] no data in any contract file, skipping")
                continue
            range_from, range_to = date_range

            # Default: one call for the whole range (unchanged from every
            # prior --all-expiries run). With --dates: one call PER
            # requested day that actually falls in this range -- every
            # other day in the range is skipped entirely, saving the bulk
            # of the per-bar replay cost for a targeted rerun (see --dates'
            # own help text for why this can't be a plain nearest-expiry
            # guess).
            days_to_run: list[date | None]
            if requested_dates is not None:
                matching_days = sorted(
                    d for d in requested_dates if range_from <= d <= range_to
                )
                if not matching_days:
                    continue
                days_to_run = []
                days_to_run.extend(matching_days)
            else:
                days_to_run = [None]  # sentinel: run the whole [range_from, range_to] window once

            for day in days_to_run:
                from_date, to_date = (day, day) if day is not None else (range_from, range_to)
                if requested_dates is not None:
                    # Real option-contract symbols (e.g. "NIFTY25093024350CE")
                    # encode their actual expiry date, so two --dates days
                    # landing in the same expiry_dir would try to re-seed the
                    # identical symbol string a second time -- collides on
                    # `uq_option_contract_symbol`, a genuinely global
                    # constraint this backtest DB shares with production
                    # schema, not something to work around by mangling
                    # symbols. Instead, each --dates day gets a fully fresh
                    # database (this is already how separate --shard-count
                    # processes stay isolated from each other; here it's the
                    # same reset applied between sequential calls within one
                    # process) -- cheap, since a single day's seed+replay is
                    # small regardless.
                    Base.metadata.drop_all(engine)
                    Base.metadata.create_all(engine)
                trades_by_mode, rejected, reasons, signals = _run_single_backtest(
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
                    exit_modes=exit_modes,
                    total_lots=args.total_lots,
                    underlying_series=underlying_series,
                    target_multiplier=args.target_multiplier,
                )
                total_risk_rejected += rejected
                total_risk_rejected_reasons.update(reasons)
                total_signals_all += signals
                for mode in exit_modes:
                    mode_trades = trades_by_mode[mode]
                    all_trades_by_mode[mode].extend(mode_trades)
                    mode_csv = _out_csv_for(mode)
                    _append_trade_csv_rows(
                        mode_trades, mode_csv, write_header=not wrote_header_by_mode[mode]
                    )
                    wrote_header_by_mode[mode] = True
                label = f"{expiry_date.isoformat()}" if day is None else (
                    f"{day.isoformat()} via expiry {expiry_date.isoformat()}"
                )
                totals_str = ", ".join(
                    f"{mode}={len(all_trades_by_mode[mode])}" for mode in exit_modes
                )
                print(
                    f"  [{label}] "
                    + ", ".join(f"{mode}={len(trades_by_mode[mode])}" for mode in exit_modes)
                    + f" trade(s) appended ({totals_str} total so far)"
                )
    elif args.pairs:
        parsed_pairs: list[tuple[date, date]] = []
        for chunk in args.pairs.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            day_str, expiry_str = chunk.split(":")
            parsed_pairs.append(
                (date.fromisoformat(day_str.strip()), date.fromisoformat(expiry_str.strip()))
            )
        if args.shard_count > 1:
            parsed_pairs = parsed_pairs[args.shard_index :: args.shard_count]
            print(
                f"Shard {args.shard_index}/{args.shard_count}: "
                f"{len(parsed_pairs)} of the full --pairs list assigned to this process."
            )
        print(
            f"Replaying {len(parsed_pairs)} exact (day, expiry) pair(s) for "
            f"{args.strategy}/{args.underlying} ..."
        )
        wrote_header_by_mode = {m: False for m in exit_modes}
        for day, expiry in parsed_pairs:
            expiry_date, expiry_dir = _discover_expiry_dir(
                data_dir, args.underlying, day, expiry, args.options_subdir
            )
            # Each pair gets a fresh schema, same reasoning as --dates: real
            # option-contract symbols are globally unique on their own
            # broker string, so two pairs sharing an expiry (or the DB
            # otherwise accumulating state across calls) would collide or
            # silently carry state between what should be independent
            # single-day replays.
            Base.metadata.drop_all(engine)
            Base.metadata.create_all(engine)
            trades_by_mode, rejected, reasons, signals = _run_single_backtest(
                underlying=args.underlying,
                strategy_type=args.strategy,
                from_date=day,
                to_date=day,
                expiry_date=expiry_date,
                expiry_dir=expiry_dir,
                all_underlying_bars=all_underlying_bars,
                diagnostics=diagnostics,
                session_factory=session_factory,
                fast=args.fast,
                exit_modes=exit_modes,
                total_lots=args.total_lots,
                underlying_series=underlying_series,
                target_multiplier=args.target_multiplier,
            )
            total_risk_rejected += rejected
            total_risk_rejected_reasons.update(reasons)
            total_signals_all += signals
            for mode in exit_modes:
                mode_trades = trades_by_mode[mode]
                all_trades_by_mode[mode].extend(mode_trades)
                mode_csv = _out_csv_for(mode)
                _append_trade_csv_rows(
                    mode_trades, mode_csv, write_header=not wrote_header_by_mode[mode]
                )
                wrote_header_by_mode[mode] = True
            totals_str = ", ".join(
                f"{mode}={len(all_trades_by_mode[mode])}" for mode in exit_modes
            )
            print(
                f"  [{day.isoformat()} via expiry {expiry_date.isoformat()}] "
                + ", ".join(f"{mode}={len(trades_by_mode[mode])}" for mode in exit_modes)
                + f" trade(s) appended ({totals_str} total so far)"
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
        all_trades_by_mode, total_risk_rejected, total_risk_rejected_reasons, total_signals_all = (
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
                exit_modes=exit_modes,
                total_lots=args.total_lots,
                underlying_series=underlying_series,
                target_multiplier=args.target_multiplier,
            )
        )

    for mode in exit_modes:
        mode_label = _current_mode_label(args.strategy) if mode == "current" else mode
        if multi_mode:
            print(f"\n{'#' * 78}\n# exit-mode: {mode_label}\n{'#' * 78}")
        elif mode == "current":
            print(f"exit-mode: {mode_label}")
        _print_report(
            all_trades_by_mode[mode], total_risk_rejected, total_risk_rejected_reasons,
            total_signals_all,
        )
        _write_trade_csv(all_trades_by_mode[mode], _out_csv_for(mode))


if __name__ == "__main__":
    main()
