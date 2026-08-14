"""Ops-Hardening Phase 3: app.modules.reporting.exporter -- pure day-boundary/
sheet-naming logic plus the Excel-write path (sheet routing, idempotent
append, file-lock fallback), driven with synthetic `TradeLogRow`s against a
`tmp_path`-backed `REPORTS_DIR` rather than the DB -- `fetch_completed_trades_
for_day`'s own DB-query correctness is covered separately in
tests/integration/test_trade_log_export_query.py.
"""

from __future__ import annotations

import csv
import uuid
from datetime import UTC, date, datetime

import openpyxl
import pytest

from app.modules.reporting import exporter
from app.modules.reporting.exporter import (
    TradeLogRow,
    _day_bounds_utc,
    _sanitize_sheet_name,
    export_trade_log_for_workspace,
)


@pytest.fixture(autouse=True)
def _reports_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "REPORTS_DIR", tmp_path)
    return tmp_path


def _row(
    *,
    workspace_id: uuid.UUID | None = None,
    underlying: str = "NIFTY",
    expiry: date = date(2026, 8, 18),
    strategy: str = "orb",
    trade_id: uuid.UUID | None = None,
    pnl: float = 500.0,
) -> TradeLogRow:
    return TradeLogRow(
        trade_outcome_id=trade_id or uuid.uuid4(),
        workspace_id=workspace_id or uuid.uuid4(),
        strategy_name=strategy,
        execution_mode="auto",
        underlying_symbol=underlying,
        contract_symbol=f"{underlying}{expiry.strftime('%d%b%y').upper()}C24000",
        option_type="CE",
        strike=24000.0,
        expiry_date=expiry,
        side="buy",
        qty=25,
        entry_price=80.0,
        entry_time_utc=datetime(2026, 8, 18, 5, 0, tzinfo=UTC),
        exit_price=100.0,
        exit_time_utc=datetime(2026, 8, 18, 6, 0, tzinfo=UTC),
        exit_reason="target",
        realized_pnl=pnl,
        slippage=1.5,
    )


# -- _day_bounds_utc ----------------------------------------------------------


def test_day_bounds_are_ist_midnight_converted_to_utc():
    start, end = _day_bounds_utc(date(2026, 8, 18))

    # IST is UTC+5:30 -- 00:00 IST on the 18th is 18:30 UTC on the 17th.
    assert start == datetime(2026, 8, 17, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 8, 18, 18, 30, tzinfo=UTC)


# -- _sanitize_sheet_name -------------------------------------------------


def test_sanitize_sheet_name_replaces_illegal_excel_characters():
    assert _sanitize_sheet_name("NIFTY: 18/08/2026") == "NIFTY- 18-08-2026"


def test_sanitize_sheet_name_truncates_to_31_chars():
    assert len(_sanitize_sheet_name("X" * 50)) == 31


# -- export_trade_log_for_workspace ---------------------------------------


def test_zero_rows_returns_none_and_touches_no_file(tmp_path):
    result = export_trade_log_for_workspace(uuid.uuid4(), [], date(2026, 8, 18))

    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_creates_workbook_with_correct_sheet_and_headers(tmp_path):
    workspace_id = uuid.uuid4()
    row = _row(workspace_id=workspace_id)

    path = export_trade_log_for_workspace(workspace_id, [row], date(2026, 8, 18))

    assert path == tmp_path / f"trade_log_{workspace_id}.xlsx"
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["NIFTY 2026-08-18"]
    ws = wb["NIFTY 2026-08-18"]
    assert ws.cell(row=1, column=1).value == "Strategy"
    assert ws.cell(row=2, column=1).value == "orb"
    assert ws.cell(row=2, column=15).value == str(row.trade_outcome_id)


def test_routes_different_cycles_to_different_sheets(tmp_path):
    workspace_id = uuid.uuid4()
    this_week = _row(workspace_id=workspace_id, underlying="NIFTY", expiry=date(2026, 8, 18))
    next_week = _row(workspace_id=workspace_id, underlying="NIFTY", expiry=date(2026, 8, 25))
    other_underlying = _row(
        workspace_id=workspace_id, underlying="BANKNIFTY", expiry=date(2026, 8, 25)
    )

    path = export_trade_log_for_workspace(
        workspace_id, [this_week, next_week, other_underlying], date(2026, 8, 18)
    )

    wb = openpyxl.load_workbook(path)
    assert set(wb.sheetnames) == {
        "NIFTY 2026-08-18",
        "NIFTY 2026-08-25",
        "BANKNIFTY 2026-08-25",
    }
    assert wb["NIFTY 2026-08-18"].max_row == 2  # header + 1 row
    assert wb["NIFTY 2026-08-25"].max_row == 2
    assert wb["BANKNIFTY 2026-08-25"].max_row == 2


def test_reexporting_the_same_trades_does_not_duplicate_rows(tmp_path):
    workspace_id = uuid.uuid4()
    row = _row(workspace_id=workspace_id)

    export_trade_log_for_workspace(workspace_id, [row], date(2026, 8, 18))
    path = export_trade_log_for_workspace(workspace_id, [row], date(2026, 8, 18))

    wb = openpyxl.load_workbook(path)
    assert wb["NIFTY 2026-08-18"].max_row == 2  # still just header + 1 row


def test_new_trades_append_alongside_already_exported_ones(tmp_path):
    workspace_id = uuid.uuid4()
    first = _row(workspace_id=workspace_id)
    second = _row(workspace_id=workspace_id)

    export_trade_log_for_workspace(workspace_id, [first], date(2026, 8, 18))
    path = export_trade_log_for_workspace(workspace_id, [first, second], date(2026, 8, 18))

    wb = openpyxl.load_workbook(path)
    assert wb["NIFTY 2026-08-18"].max_row == 3  # header + 2 distinct trades


def test_separate_workspaces_get_separate_files(tmp_path):
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()

    path_a = export_trade_log_for_workspace(ws_a, [_row(workspace_id=ws_a)], date(2026, 8, 18))
    path_b = export_trade_log_for_workspace(ws_b, [_row(workspace_id=ws_b)], date(2026, 8, 18))

    assert path_a != path_b
    assert path_a.exists() and path_b.exists()


def test_permission_error_falls_back_to_csv(tmp_path, monkeypatch):
    def _raise_permission_error(self, path):
        raise PermissionError("file is open in Excel")

    monkeypatch.setattr(openpyxl.Workbook, "save", _raise_permission_error)

    workspace_id = uuid.uuid4()
    row = _row(workspace_id=workspace_id)

    result_path = export_trade_log_for_workspace(workspace_id, [row], date(2026, 8, 18))

    assert result_path is not None
    assert result_path.suffix == ".csv"
    assert result_path.exists()
    # The real .xlsx must never have been left in a half-written state.
    assert not (tmp_path / f"trade_log_{workspace_id}.xlsx").exists()

    with result_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0][0] == "Underlying"
    assert rows[1][0] == "NIFTY"
    assert rows[1][2] == "orb"  # Strategy column, shifted by the two prepended columns
