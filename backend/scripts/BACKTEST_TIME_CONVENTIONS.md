# Backtest time conventions — IST everywhere

**Rule: every timestamp in the backtest pipeline and in any analysis of its
output is Asia/Kolkata (IST, UTC+05:30). Never rely on the host clock.**

The backtest VM (and the OCI live box) run on **UTC**. Market-hours logic,
opening-range anchoring, EOD square-off, entry-hour analysis, and
day-of-week bucketing are all meaningless in UTC. Getting this wrong
silently shifts every "9:30 breakout" / "12:00 IST dead zone" / "Tuesday
expiry" result by 5.5 hours.

## What is already correct (verified 2026-08-28)

`backend/scripts/run_backtest.py`:
- `IST` imported from `app.core.clock`; historical CSV rows parsed with
  `.replace(tzinfo=IST)` (line ~430) — every `Bar.ts` is IST-aware.
- `session_start = datetime.combine(from_date, time(9, 0), tzinfo=IST)`.
- `EOD_CUTOFF = time(15, 9)` compared via `bar.ts.time()` (IST time-of-day).
- ORB opening range anchored to `time(9, 15)` IST off the bar's own
  timestamp.
- Trade CSV columns `entry_time` / `exit_time` written via
  `to_ist(...).isoformat()` → always carry `+05:30`.
- The only `datetime.now(UTC)` uses are idempotency keys and
  snapshot-age checks that never influence entry/exit timing (and the
  timing-relevant ones are corrected by `_correct_timestamps`).

## Rules for any new analysis script (`analyze_*.py`)

1. Parse the CSV time columns as UTC then **immediately convert**:
   `pd.to_datetime(col, utc=True).dt.tz_convert("Asia/Kolkata")`.
   The `+05:30` suffix means `utc=True` parses correctly; the convert is
   what makes `.dt.hour`, `.dt.day_name()`, `.dt.floor("30min")` report IST.
2. Never call `.dt.hour` / `.dt.date` / `.dt.day_name()` on a naive or
   still-UTC series.
3. When printing "as of HH:MM", label it `IST` explicitly.
4. If a script ever needs "today", use IST:
   `pd.Timestamp.now(tz="Asia/Kolkata")`, never `date.today()` /
   `datetime.now()` on the host.

## Rules for any new backtest / strategy / exit-logic code

- Time-of-day gates (entry cutoff, time-stop, no-entry windows) compare
  `to_ist(bar.ts).time()` against an IST `time(...)` literal — never a
  naive `.time()` on a possibly-UTC datetime, never `now()`.
- New `TradeProposal` time fields (e.g. `time_stop_ist`) are IST `time`
  objects, documented as IST at the field.
