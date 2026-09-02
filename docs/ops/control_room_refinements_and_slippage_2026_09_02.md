# Control Room refinements + entry/exit slippage — session record (2026-09-02)

**Status: implemented and self-verified in this session. NOT committed, pushed, or deployed** — holding per explicit instruction, pending review from a second concurrent session (see "Concurrent session" section below) before any combined save/sync.

## Objective / context

A refinement pass on six open questions plus a set of Control Room UI requests. Most of the six questions resolved to "no code change" (documented below for the record). Two pieces of real work came out of it:

1. **Control Room UI fixes** (Total Trades scoping, no-decimal display, P&L card redesign, CE/PE filter, a real Rejected-vs-Cancelled mislabel bug, and a regression the UI work itself introduced in the Paper sub-ribbon).
2. **Entry + exit slippage tracking**, newly requested: exit-side slippage already existed in the DB but was never surfaced in the UI; entry-side slippage didn't exist at all. Built to mirror the existing exit-side design exactly, and now displayed per-row in the Control Room trade table.

## Part 1 — Control Room UI (implemented earlier in this session, already stable)

- **Total Trades**: now closed-trades-only (previously the client-side fallback counted open/closing rows too, before the real `report.trade_count` loaded).
- **No-decimal display**: every currency value on the page now rounds via a shared `fmtAmt()` helper.
- **P&L card redesign**: `[Realized Profit] / [Unrealized P&L] / [Total Cost + Win Rate, stacked]`, single-row-forced metrics strip.
- **CE/PE filter** added to Today's Trades (both Live and Paper).
- **Status labeling**: `closing` → "Exit order sent — awaiting trigger" (was "Closing (exit sent)").
- **Real bug fixed**: a cancelled order (e.g. a resting SL cancelled because the position exited via structure-break instead) was being collapsed into the same "Rejected" label/badge as a genuine broker rejection. Added `'cancelled'` as its own `TradeRowStatus`, label, and neutral badge.
- **Regression found and fixed same session**: the single-row metrics-strip fix shrank the *primary* strip's box widths, but the Paper sub-ribbon's own separate CSS sizing was left larger than the primary strip and had no narrow-box override at all — so the sub-ribbon silently overflowed and its right-hand boxes scrolled out of view (looked like "half the metrics missing" for paper). Fixed by shrinking the sub-ribbon's own sizes below the primary strip's.

Files: `frontend/src/features/control-room/ControlRoomPage.tsx`, `frontend/src/index.css`, `frontend/src/shared/trades/buildTradeRows.ts`.

## Part 2 — Entry + exit slippage (implemented this turn)

**Design**: reuses `app/core/pnl.py`'s existing `signed_pnl()` — the same formula exit-side slippage already used. Entry-side slippage is `signed_pnl(actual_fill_price, intended_entry_price, qty, side)` — note the argument order is **deliberately swapped** relative to the exit-side call. Exit slippage is positive when the exit fills *better* than the trigger price that justified it; for an entry, "better" is paying less (long) than intended, which requires swapping which price comes first so "positive = favorable" holds for both. This was caught and fixed during the plan-review pass, before any code was written — see `docs/ops/` git history / the plan file if you want the full reasoning trail (`~/.claude/plans/adaptive-puzzling-coral.md` on this machine, not part of the repo).

**Backend**:
- New column `Position.entry_slippage` (`Numeric(14,2)`, nullable) — migration `backend/migrations/versions/0031_position_entry_slippage.py`, additive-only, round-tripped clean (`upgrade` → `downgrade -1` → `upgrade` all verified).
- Computed once at open time in `execution_engine/paper/service.py`'s `_open_position_from_fill` (the only construction site of `Position`), alongside where `entry_price` itself is already set.
- Exit-side slippage (`TradeOutcome.slippage` / `PositionExitLeg.slippage`) already existed and was already correctly computed — this session only **exposed** it: `api/v1/execution.py`'s `PositionOut`/`PositionLegOut` now carry `entry_slippage`/`exit_slippage`/`slippage` fields, copied from data that was already being batch-loaded (`outcomes_by_position`), no new query.
- Reporting: `reporting/service.py`'s `PerformanceStats` gained a new `total_entry_slippage` field (existing `total_slippage` left untouched — still exit-only, to avoid changing an existing field's meaning for any downstream consumer). New `_entry_slippage_by_position` helper does one extra lightweight query per report build. Threaded through `api/v1/reports.py`'s `PerformanceStatsOut` and `reporting/exporter.py`'s trade-log Excel export (new "Entry Slippage" column, additive — the exporter resolves columns by name, so an old exported sheet is unaffected).

**Frontend**:
- `shared/api/types.ts`: `entry_slippage`/`exit_slippage` on `PositionOut`, `slippage` on `PositionLegOut`, `total_entry_slippage` on `PerformanceStatsOut`.
- `shared/trades/buildTradeRows.ts`: `entrySlippage`/`exitSlippage: number | null` added to `TradeRow`, populated only for position rows (null for approval/order-only rows — nothing to measure yet).
- `ControlRoomPage.tsx`: displayed as a small muted subvalue under the existing Entry Price cell (entry slippage) and P&L cell (exit slippage, once closed), reusing the `pnl-positive`/`pnl-negative` color convention. Staged/multi-leg positions get a new "Slippage" column in the per-leg expanded table.

## Verification performed

- `alembic upgrade head` / `downgrade -1` / `upgrade head` — clean round-trip.
- Full backend suite: **1488 passed** (most recent run, includes the concurrent session's own changes/tests — see below). Two new dedicated tests added for the entry-slippage sign convention specifically (`test_entry_slippage_sign_is_favorable_positive_unfavorable_negative`, forces a real nonzero fill-vs-intended gap via `queue_fill_scenario` and asserts the sign both ways — a zero-slippage test alone can't catch an argument-order mistake). One pre-existing test's call-count assertion updated to account for the new third `signed_pnl` call at open.
- `ruff check .` (project-standard, respects `migrations/versions` exclusion) — clean.
- `mypy app tests` — clean **for every file this session touched**. It currently reports 8 errors, all inside the concurrent session's own in-progress hunks (see below) — not introduced by this work, confirmed by hunk-boundary analysis (`git diff <file> | grep '^@@'`).
- Frontend `npm run build` — clean, twice (once after the UI batch, once after the slippage display work).
- **Not done**: live-rendered visual confirmation of actual nonzero slippage numbers in the browser — the local dev DB has no active trades/positions right now (no strategies running against live market data locally), so there's nothing to render. The code path is exercised end-to-end by the backend integration tests instead, and the display logic directly mirrors patterns already visually verified earlier this session (fmtAmt, conditional-null rendering, pnl-positive/negative coloring).

## Concurrent session — read this before syncing

Partway through this work, `git status` revealed a **second, independent set of uncommitted changes already in this same working tree**, not made by this session:

- `backend/app/modules/broker_adapter/base/broker_port.py`
- `backend/app/modules/broker_adapter/base/contracts.py`
- `backend/app/modules/broker_adapter/composition.py`
- `backend/app/modules/broker_adapter/mock/adapter.py`
- `backend/app/modules/broker_adapter/shoonya/adapter.py`
- `backend/app/modules/broker_adapter/shoonya/normalizer.py`
- `backend/app/modules/reconciliation/service.py`
- `backend/scripts/run_backtest.py`
- `backend/tests/unit/test_broker_composition.py`
- **`backend/app/modules/execution_engine/paper/service.py` — shared with this session.** Large hunks inside `close_position`/`_finalize_position_close`/`_apply_resolved_pending_exit_order`/`current_contract_price` (old-file lines ~896–1837) are the other session's; this session's own hunks are isolated to `_open_position_from_fill` (~line 410–455) and are far from theirs. No overlapping lines confirmed via `git diff | grep '^@@'`.

The user confirmed this is their own other active session, doing related broker-execution work. **Nothing in the list above was touched, reviewed, or fixed by this session** — the 8 mypy errors mentioned above belong entirely to that other work.

**Recommended next step**: hand this document to that other session for it to review this session's diff (`git diff` scoped to the files listed under Part 1/Part 2 above) alongside its own, confirm no unexpected interaction in the shared `service.py` file, then do one final combined `pytest`/`ruff`/`mypy`/`npm run build` pass across everything together before any commit — the last full-suite run here (1488 passed) already includes both sessions' code, which is a good sign, but should be re-confirmed after the other session's own work is finalized, since it may still be in progress.
