"""Ops-Hardening Phase 3. Daily trade-log Excel export — pure data-query +
file-write logic, no threading (`export_scheduler.py` is the thread that
calls this on a schedule, same split `market_data_scheduler.py`/
`market_hours.py` already use).

**Deliberately `openpyxl` only, not `pandas`** — see this module's own
dependency comment in `pyproject.toml`: pandas pulls in numpy, whose stub
files break `mypy app tests` under this project's `python_version = "3.11"`
target, which is exactly why pandas has been kept out of the main `.venv`
since before this phase. Nothing here needs DataFrame machinery — it's a
handful of columns per row, read via a plain SQLAlchemy query.

**One workbook per workspace** (`reports/trade_log_<workspace_id>.xlsx`),
not one shared file — matches `HealthCheckScheduler`'s own precedent of
never assuming single-tenant, and never mixes different workspaces'
financial data in one file.

**One tab per (underlying, expiry_date)**, not a single global "today's
cycle" tab — each trade already carries its own cycle via its own
contract's `expiry_date` (`OptionContract`, reached through
`Position.option_contract_id`), so there's nothing to "determine": a day
where, say, this week's and next week's contracts both traded correctly
lands in two different tabs.

**Idempotent appends** — every row carries its source `TradeOutcome.id` as
its last column; before appending, a tab's existing IDs are read and
already-present ones skipped. This is what makes it safe for the scheduler
(`export_scheduler.py`) to re-trigger a day it already exported (e.g. after
a restart) without producing duplicate rows — a re-run is a no-op, not a
retry hazard, matching this project's own stated invariant ("any new write
path that could plausibly run twice needs to reason about this explicitly").
"""

from __future__ import annotations

import csv
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import openpyxl
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy.orm import Session

from app.config.settings import BACKEND_ROOT_DIR
from app.core.clock import IST, now_ist, to_ist
from app.core.db.session import session_scope
from app.domain.execution.models import Position, TradeOutcome
from app.domain.market.models import Instrument, OptionContract
from app.domain.strategy.models import StrategyConfig, StrategyRun, TradeIntent
from app.modules.strategy_engine.env_metrics import get_env_metrics

logger = logging.getLogger("app.reporting.exporter")

SessionFactory = Callable[[], AbstractContextManager[Session]]

REPORTS_DIR = BACKEND_ROOT_DIR / "reports"

_HEADERS = [
    "Strategy",
    "Execution Mode",
    "Contract Symbol",
    "Option Type",
    "Strike",
    "Side",
    "Qty",
    "Entry Price",
    "Entry Time (IST)",
    "Exit Price",
    "Exit Time (IST)",
    "Exit Reason",
    "Realized PnL",
    "Slippage",
    "VIX (at entry)",
    "PCR - OI (at entry)",
    "PCR - Volume (at entry)",
    "Trade ID (internal)",
]
_TRADE_ID_COLUMN_INDEX = len(_HEADERS)  # 1-indexed, last column


@dataclass(frozen=True)
class TradeLogRow:
    trade_outcome_id: uuid.UUID
    workspace_id: uuid.UUID
    strategy_name: str
    execution_mode: str
    underlying_symbol: str
    contract_symbol: str
    option_type: str
    strike: float
    expiry_date: date
    side: str
    qty: int
    entry_price: float
    entry_time_utc: datetime
    exit_price: float
    exit_time_utc: datetime
    exit_reason: str
    realized_pnl: float
    slippage: float
    # VIX/PCR environment metrics as of this trade's entry time, not
    # "current" -- see strategy_engine.env_metrics.get_env_metrics's own
    # docstring for the as_of_utc reconstruction this is built from. `None`
    # for any/all three when nothing was known yet at that moment (e.g. a
    # trade that predates the VIX/PCR pipeline entirely).
    vix: float | None
    pcr_oi: float | None
    pcr_vol: float | None


def _day_bounds_utc(target_date: date) -> tuple[datetime, datetime]:
    """IST-calendar-day boundaries, converted to UTC for the query filter —
    never a bare `utcnow()`/naive-date comparison, per this project's own
    timezone-strictness convention (`app.core.clock`'s own docstring).
    """
    start_ist = datetime.combine(target_date, time.min, tzinfo=IST)
    start_utc = start_ist.astimezone(UTC)
    return start_utc, start_utc + timedelta(days=1)


def fetch_completed_trades_for_day(db: Session, target_date: date) -> list[TradeLogRow]:
    """Reuses `TradeOutcome` — the same "completed trade" source of truth
    `reporting.service.build_daily_report`/`build_scorecard` already read —
    rather than re-deriving trade completion independently.
    """
    start_utc, end_utc = _day_bounds_utc(target_date)

    query_rows = (
        db.query(
            TradeOutcome,
            Position,
            OptionContract,
            Instrument,
            TradeIntent,
            StrategyRun,
            StrategyConfig,
        )
        .join(Position, TradeOutcome.position_id == Position.id)
        .join(OptionContract, Position.option_contract_id == OptionContract.id)
        .join(Instrument, OptionContract.instrument_id == Instrument.id)
        .join(TradeIntent, TradeOutcome.trade_intent_id == TradeIntent.id)
        .join(StrategyRun, TradeIntent.strategy_run_id == StrategyRun.id)
        .join(StrategyConfig, StrategyRun.strategy_config_id == StrategyConfig.id)
        .filter(TradeOutcome.closed_at >= start_utc, TradeOutcome.closed_at < end_utc)
        .order_by(TradeOutcome.closed_at)
        .all()
    )

    rows = []
    for outcome, position, contract, instrument, intent, run, config in query_rows:
        # As of this trade's own entry time, not "current" -- see
        # env_metrics.get_env_metrics's own docstring for the
        # as_of_utc reconstruction. One extra query pair per row (VIX
        # tick + option-chain snapshot); fine at this system's real
        # trade volumes, and this only ever runs once daily, off the
        # hot path.
        env = get_env_metrics(
            db, instrument.id, contract.expiry_date, as_of_utc=position.opened_at
        )
        rows.append(
            # `.value`, not a bare attribute -- these columns are all plain
            # String(N) (no native SQLAlchemy Enum type), so a value freshly
            # read back via this query's own SELECT comes back as a plain
            # `str`, not the declared StrEnum subtype (confirmed
            # empirically: a bare `.value` access AttributeErrors on the
            # read-back object even though the same field accepts an enum
            # member fine on construction). `str(...)` handles both shapes
            # uniformly rather than assuming one.
            TradeLogRow(
                trade_outcome_id=outcome.id,
                workspace_id=intent.workspace_id,
                strategy_name=config.name,
                execution_mode=str(run.execution_mode),
                underlying_symbol=instrument.symbol,
                contract_symbol=contract.symbol,
                option_type=str(contract.option_type),
                strike=float(contract.strike),
                expiry_date=contract.expiry_date,
                side=str(position.side),
                qty=position.qty,
                entry_price=float(outcome.entry_price),
                entry_time_utc=position.opened_at,
                exit_price=float(outcome.exit_price),
                exit_time_utc=outcome.closed_at,
                exit_reason=str(outcome.exit_reason),
                realized_pnl=float(outcome.realized_pnl),
                slippage=float(outcome.slippage),
                vix=env.get("vix") if env is not None else None,
                pcr_oi=env.get("pcr_oi") if env is not None else None,
                pcr_vol=env.get("pcr_vol") if env is not None else None,
            )
        )
    return rows


def _sanitize_sheet_name(name: str) -> str:
    for ch in ":\\/?*[]":
        name = name.replace(ch, "-")
    return name[:31]


def _sheet_name_for(row: TradeLogRow) -> str:
    return _sanitize_sheet_name(f"{row.underlying_symbol} {row.expiry_date.isoformat()}")


def _row_values(row: TradeLogRow) -> list:
    return [
        row.strategy_name,
        row.execution_mode,
        row.contract_symbol,
        row.option_type,
        row.strike,
        row.side,
        row.qty,
        row.entry_price,
        to_ist(row.entry_time_utc).replace(tzinfo=None),
        row.exit_price,
        to_ist(row.exit_time_utc).replace(tzinfo=None),
        row.exit_reason,
        row.realized_pnl,
        row.slippage,
        row.vix,
        row.pcr_oi,
        row.pcr_vol,
        str(row.trade_outcome_id),
    ]


def _existing_trade_ids(ws: Worksheet) -> set[str]:
    ids: set[str] = set()
    for row_cells in ws.iter_rows(min_row=2):
        cell = row_cells[_TRADE_ID_COLUMN_INDEX - 1]
        if cell.value:
            ids.add(str(cell.value))
    return ids


def _get_or_create_sheet(wb: openpyxl.Workbook, sheet_name: str) -> Worksheet:
    if sheet_name in wb.sheetnames:
        return wb[sheet_name]
    ws = wb.create_sheet(title=sheet_name)
    ws.append(_HEADERS)
    return ws


def _write_csv_fallback(
    workspace_id: uuid.UUID, rows: list[TradeLogRow], target_date: date
) -> Path:
    """The file-lock guard's actual safety net — same row shape as the
    Excel append, just to a fresh, never-locked file, so a `.xlsx` left open
    on the desktop at 15:35 never costs the day's records.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_ist().strftime("%H%M%S")
    path = REPORTS_DIR / f"trade_log_{workspace_id}_{target_date.isoformat()}_fallback_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Underlying", "Expiry", *_HEADERS])
        for row in rows:
            writer.writerow([row.underlying_symbol, row.expiry_date.isoformat(), *_row_values(row)])
    return path


def export_trade_log_for_workspace(
    workspace_id: uuid.UUID, rows: list[TradeLogRow], target_date: date
) -> Path | None:
    """`rows` must already be scoped to one workspace. Returns the path
    actually written to (the `.xlsx`, or a CSV fallback on a file-lock
    error), or `None` if there was nothing to export.
    """
    if not rows:
        logger.info("No trades to export for workspace %s on %s", workspace_id, target_date)
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"trade_log_{workspace_id}.xlsx"

    is_new_workbook = not path.exists()
    wb = openpyxl.load_workbook(path) if not is_new_workbook else openpyxl.Workbook()
    if is_new_workbook:
        # A freshly-created Workbook() always starts with exactly one
        # default "Sheet" -- safe to drop immediately since a real sheet is
        # about to be created below (rows is non-empty, checked above).
        del wb["Sheet"]

    by_sheet: dict[str, list[TradeLogRow]] = defaultdict(list)
    for row in rows:
        by_sheet[_sheet_name_for(row)].append(row)

    appended = 0
    for sheet_name, sheet_rows in by_sheet.items():
        ws = _get_or_create_sheet(wb, sheet_name)
        existing_ids = _existing_trade_ids(ws)
        for row in sheet_rows:
            if str(row.trade_outcome_id) in existing_ids:
                continue
            ws.append(_row_values(row))
            appended += 1

    if appended == 0:
        logger.info(
            "All %d trade(s) for workspace %s on %s were already exported — nothing new to append",
            len(rows),
            workspace_id,
            target_date,
        )
        return path

    try:
        wb.save(path)
    except PermissionError:
        fallback_path = _write_csv_fallback(workspace_id, rows, target_date)
        logger.warning(
            "Could not save %s (likely open in another program) — wrote a CSV fallback "
            "to %s instead so today's %d trade(s) aren't lost",
            path,
            fallback_path,
            len(rows),
        )
        return fallback_path

    logger.warning(
        "Exported %d new trade row(s) for workspace %s to %s", appended, workspace_id, path
    )
    return path


def export_completed_trades_for_day(
    target_date: date | None = None,
    *,
    session_factory: SessionFactory = session_scope,
) -> None:
    """Entry point `export_scheduler.py`'s daily trigger calls. `target_date`
    defaults to `now_ist().date()` — always IST, never a bare `utcnow()`
    date, per this project's own timezone-strictness convention.
    """
    resolved_date = target_date if target_date is not None else now_ist().date()

    with session_factory() as db:
        rows = fetch_completed_trades_for_day(db, resolved_date)

    if not rows:
        logger.info("No trades to export for %s", resolved_date)
        return

    by_workspace: dict[uuid.UUID, list[TradeLogRow]] = defaultdict(list)
    for row in rows:
        by_workspace[row.workspace_id].append(row)

    for workspace_id, workspace_rows in by_workspace.items():
        export_trade_log_for_workspace(workspace_id, workspace_rows, resolved_date)
