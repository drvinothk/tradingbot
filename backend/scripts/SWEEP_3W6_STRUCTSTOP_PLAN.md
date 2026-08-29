# Sweep #3 W6 (chart-based structure stop) + W7 (stop × TSL grid)

Status: **planned, not started.** Extended-plan merge 2026-08-29 ~17:45 IST
(the "5th framework strategy" idea is DROPPED). Run before the e4 backtest VM
(`129.159.226.106`, `ubuntu`) terminates **night of 2026-08-31 (~05:30 IST
Sep 1 hard cutoff)**. Read `BACKTEST_LEARNINGS.md`'s "CANONICAL RELIABLE-
BACKTEST SETUP" section first — every non-negotiable there applies, plus its
`## 2026-08-29 (~17:15 IST)` W4/W5 results entry for the numbers this builds on.

**W7 has NO code change — launch it first, immediately.** W6 needs the
`run_backtest.py` harness edit (Part A) + `ruff`/`mypy` + a byte-identical
smoke before its sweep can run. Distinct DB prefixes (`s3w7_`, `s3w6_`) → both
run concurrently.

SSH: `ssh -i "D:\Documents\Trading Bot_Oracle\ssh-key-2026-08-03_Pvt Key.key" ubuntu@129.159.226.106`
Repo on VM: `~/trading-bot/backend` (no git — deploy scripts via `scp`).
Venvs: `./.venv` (backtests), `~/an_venv` (pandas/numpy analysis, no scipy).

---

## Why

W4 (done) on the 26 `d_pdt_w65` trades (`orb_conviction` +
`{require_prior_day_trend:true, max_or_range_nifty_points:65}`,
`--exit-mode current`):
- fixed **premium target** inert (trail catches everything first); removing it ≈ −₹8/lot.
- **looser premium stop is the dominant lever** — monotonic −10→−12→−15→−18 %:
  `stop_pct:0.18` → 73 % win, +₹456/lot, PF 12.1, maxDD ₹425, P(mean≤0)=0.000.
- **trail-arm timing = a win% ⇄ ₹ dial**: arm +6 % → ~81 % win / +₹256–280;
  arm +12–14 % → ~65 % win / +₹280–460; **arm +10 % is a dead zone**;
  `trail_lock_fraction` 0.4→0.6 is free.
- target *re-activates* once the stop is loose: `stop 0.15 + target 0.30` → +₹515/lot (65 % win).

**W6** — replace the hard-wired opening-range structure level with a stop
anchored to **chart structure — the nearest support/resistance**: a recent
swing candle low/high, **and** classic floor pivots (S1/R1/S2/R2), swept
side-by-side. Backtest-only `run_backtest.py` harness change, no strategy code.

**W7** — a proper **stop ∈ {15,18}% × trail-arm ∈ {6,8,10,12,14}% × lock ∈
{0.4,0.6}** grid (no fixed target). W4 only tested arm +6/+8 at stop 15/18 and
stop 18 has no arm ladder — "stop 18 + late arm +14 %" (likely the max-₹ cell,
by analogy with the stop-12 ladder) is untested. Pure `--strategy-params`.

`--exit-mode current` already exits on the *underlying* crossing
`trade_intent.structure_level` (± ATR buffer, persistence, bar-close confirm) —
its "structure-break" step. For `orb_conviction` that level is hard-wired to
the opening-range boundary (`app/modules/strategy_engine/strategies/orb.py`
lines ~169-171). W6 makes that level selectable at reconstruction time.

Everything needed is already in scope at the `mode == "current"` call site in
`run_backtest.py` (~line 2555):
- `all_underlying_bars: list[Bar]` — full OHLC (`Bar` NamedTuple:
  ts/open/high/low/close/volume/oi, defined ~line 423), loaded whole
  regardless of shard (~line 2870) -> swing low/high.
- `compute_floor_pivots`, `prior_day_ohlc`, `pivots_by_date` (from
  `backtest_pivots.py`, already imported ~line 245; `pivots_by_date` computed
  ~line 2605 for the legs path — hoist it above the `current` branch) ->
  S1/R1/S2/R2.
- `option_type_by_intent[intent_id]` — dict built ~line 2389, populated
  ~line 2500, in scope at the `current` branch — CE vs PE.
- `_reconstruct_exit_current` reads `structure_level` from
  `trade_intent.structure_level` at lines ~1576-1580 — the single override
  point. Buffer / persistence / bar-close / step-order are all downstream of
  that variable and stay untouched.

---

## Part A — harness change (`backend/scripts/run_backtest.py`, ~55 lines)

**A1. New helper** (near the other pure helpers, e.g. after `_load_underlying_bars`):

```python
def _swing_structure_level(
    underlying_bars: list[Bar], entry_ts: datetime, *, lookback: int, is_ce: bool
) -> float | None:
    """Recent swing low (CE) / high (PE): the min low / max high over the last
    `lookback` completed underlying bars strictly before `entry_ts`. `None`
    if the window is empty (entry too near session open). Plain min/max over
    N bars = "the bottom/top of the nearby support/resistance candle" — one
    clean param, no fractal-strength knob to overfit at n=26."""
    window = [b for b in underlying_bars if b.ts < entry_ts][-lookback:]
    if not window:
        return None
    return min(b.low for b in window) if is_ce else max(b.high for b in window)
```

**A2. Two argparse flags** (in the `--exit-mode` block area, ~line 2725):

```python
parser.add_argument(
    "--structure-stop-mode", default="or_boundary",
    choices=["or_boundary", "swing", "pivot_s1r1", "pivot_s2r2"],
    help="`--exit-mode current` only. What the structure-break exit's level is "
    "anchored to. 'or_boundary' (default) = today's behaviour, byte-identical: "
    "the strategy's own structure_level (opening-range boundary for ORB). "
    "'swing' = min low (CE) / max high (PE) of the last --swing-lookback "
    "underlying bars before entry. 'pivot_s1r1'/'pivot_s2r2' = classic "
    "floor-pivot S1/R1 or S2/R2 off the prior day's OHLC (backtest_pivots.py). "
    "Ignored by every other exit mode.",
)
parser.add_argument(
    "--swing-lookback", type=int, default=10,
    help="Only for --structure-stop-mode swing: how many prior underlying "
    "bars (1-min) the swing low/high is taken over. Default 10.",
)
```

**A3. `_reconstruct_exit_current` signature + body.** Add kwarg
`structure_level_override: float | None = None` (end of the keyword-only
params, ~line 1498). In the body where `structure_level` is set (~1576-1580):

```python
structure_level = (
    Decimal(str(structure_level_override))
    if structure_level_override is not None
    else (
        Decimal(str(trade_intent.structure_level))
        if trade_intent.structure_level is not None
        else None
    )
)
```

Nothing else in that function changes.

**A4. Call site** (`mode == "current"` branch, ~line 2555). Before the
`_reconstruct_exit_current(...)` call, compute the override:

```python
is_ce = option_type_by_intent[intent_id] == DomainOptionType.CE
if args.structure_stop_mode == "swing":
    structure_level_override = _swing_structure_level(
        all_underlying_bars, trade_intent.created_at,
        lookback=args.swing_lookback, is_ce=is_ce,
    )
elif args.structure_stop_mode in ("pivot_s1r1", "pivot_s2r2"):
    entry_date = trade_intent.created_at.date()
    if entry_date not in pivots_by_date:
        ohlc = prior_day_ohlc(all_underlying_bars, entry_date)
        pivots_by_date[entry_date] = compute_floor_pivots(*ohlc) if ohlc else None
    pv = pivots_by_date[entry_date]
    if pv is None:
        structure_level_override = None
    elif args.structure_stop_mode == "pivot_s1r1":
        structure_level_override = pv.s1 if is_ce else pv.r1
    else:
        structure_level_override = pv.s2 if is_ce else pv.r2
else:  # or_boundary
    structure_level_override = None
```

then pass `structure_level_override=structure_level_override` into the call.
`pivots_by_date` is already a dict in scope; make sure it's initialised before
this branch (it is — `pivots_by_date: dict[...] = {}` ~line 2390).

`None` override (empty swing window, first-day pivot) falls back to the
strategy's own `structure_level` — never crash, never skip a trade.

**A5. Validate.** `ruff check .` and `mypy app tests` clean. Smoke:
```
./.venv/bin/python scripts/run_backtest.py --strategy orb_conviction --underlying NIFTY \
  --all-expiries --options-subdir options_1min_past --underlying-source alice_index \
  --exit-mode current --fast --near-expiry-days 6 --structure-stop-mode or_boundary \
  --strategy-params '{"require_prior_day_trend":true,"max_or_range_nifty_points":65}' \
  --shard-count 18 --shard-index 0 --db-suffix w6smoke --out-csv /tmp/w6smoke_s0.csv
```
Must be byte-identical to `sweep3w4_exitgrid_20260829T074553Z/x_baseline_s0.csv`
(the override path is inert for `or_boundary`).

---

## Part B — W6 sweep driver (`backend/scripts/run_refined_sweep_3w6_structstop.sh`)

Clone `run_refined_sweep_3w4_sl.sh` exactly (it has the `reap_dbs()` +
`trap reap_dbs EXIT` + per-config reap already). Change:
- `DB_PREFIX="trading_bot_backtest_s3w6_"`  and  `--db-suffix "s3w6_${name}_s${i}"`
- `RESULTS_DIR=.../sweep3w6_structstop_${STAMP}`, status/log files `sweep3w6_*`
- The per-config `run_backtest.py` line gains `--structure-stop-mode` and
  `--swing-lookback` — so the CONFIGS array rows carry **`name|extraflags|params`**
  (3 fields), and the loop does `--strategy-params "$params" $extraflags`.

Base params on **every** config (the W4 winner as the premium backstop):
`G='"require_prior_day_trend":true,"max_or_range_nifty_points":65,"stop_pct":0.15'`

```
CONFIGS=(
  "s_or_baseline|--structure-stop-mode or_boundary|{$G}"
  "s_swing05|--structure-stop-mode swing --swing-lookback 5|{$G}"
  "s_swing10|--structure-stop-mode swing --swing-lookback 10|{$G}"
  "s_swing15|--structure-stop-mode swing --swing-lookback 15|{$G}"
  "s_swing20|--structure-stop-mode swing --swing-lookback 20|{$G}"
  "s_swing30|--structure-stop-mode swing --swing-lookback 30|{$G}"
  "s_piv_s1r1|--structure-stop-mode pivot_s1r1|{$G}"
  "s_piv_s2r2|--structure-stop-mode pivot_s2r2|{$G}"
  "s_swing10_buf0|--structure-stop-mode swing --swing-lookback 10|{$G,\"structure_break_atr_multiplier\":0}"
  "s_swing10_buf10|--structure-stop-mode swing --swing-lookback 10|{$G,\"structure_break_atr_multiplier\":1.0}"
  "s_swing10_nostop|--structure-stop-mode swing --swing-lookback 10|{$G_noStop}"
  "s_piv_s1r1_nostop|--structure-stop-mode pivot_s1r1|{$G_noStop}"
  "s_or_nostop|--structure-stop-mode or_boundary|{$G_noStop}"
)
```
where `G_noStop='"require_prior_day_trend":true,"max_or_range_nifty_points":65,"stop_pct":0.9'`
(-90% premium stop = effectively "chart level + trail + EOD only"; do NOT use
2.0, that makes stop_price negative).

13 configs x 18 shards, `--near-expiry-days 6 --exit-mode current --fast`,
`--strategy orb_conviction --underlying NIFTY --underlying-source alice_index`.
Entries are identical across all 13 -> pure exit-overlay re-slice of the same
**26** `d_pdt_w65` trades.

Launch (VM, from `~/trading-bot/backend`, isolated prefix so it can run
alongside W7):
```
cd ~/trading-bot/backend
setsid bash scripts/run_refined_sweep_3w6_structstop.sh </dev/null >/tmp/sweep3w6_nohup.log 2>&1 & disown
```
Watch `/tmp/sweep3w6_status.log`. ETA ~1.5 h.

---

## Part C — W7 sweep driver `run_refined_sweep_3w7_slxtsl.sh` (NO code change)

Near-exact clone of `run_refined_sweep_3w4_sl.sh` (2-field `name|params` rows,
no extra flags; it already has `reap_dbs()` + `trap ... EXIT` + per-config
reap). Change: `DB_PREFIX="trading_bot_backtest_s3w7_"`,
`--db-suffix "s3w7_${name}_s${i}"`, `RESULTS_DIR=.../sweep3w7_slxtsl_${STAMP}`,
status/log `sweep3w7_*`. `--strategy orb_conviction --underlying NIFTY
--underlying-source alice_index --exit-mode current --near-expiry-days 6 --fast
--shard-count 18`.

`G='"require_prior_day_trend":true,"max_or_range_nifty_points":65'`. **No fixed
target** → `"target_pct":1.0` on every grid config (so trail-arm distance =
`entry × trail_activation_fraction`, i.e. arms at exactly +`frac`×100 % of
entry — verified against W4's `x_notgt_arm06/08/10/12/14`).

**Grid: stop ∈ {0.15,0.18} × arm ∈ {0.06,0.08,0.10,0.12,0.14} × lock ∈
{0.4,0.6}** = 20 configs `w7_s15_a06_l04 … w7_s18_a14_l06`, params
`{$G,"target_pct":1.0,"stop_pct":<s>,"trail_activation_fraction":<a>,"trail_lock_fraction":<l>}`.

**+ 4 target-interaction anchors** (zero stop18×target data exists):
```
  "w7_s15_tgt40|{$G,\"stop_pct\":0.15,\"target_pct\":0.40}"
  "w7_s18_tgt30|{$G,\"stop_pct\":0.18,\"target_pct\":0.30}"
  "w7_s18_tgt40|{$G,\"stop_pct\":0.18,\"target_pct\":0.40}"
  "w7_s18_tgt30_a14_l06|{$G,\"stop_pct\":0.18,\"target_pct\":0.30,\"trail_activation_fraction\":0.42,\"trail_lock_fraction\":0.6}"
```
(`w7_s18_tgt30_a14_l06`: target 0.30 so `trail_activation_fraction:0.42` arms
at `0.30×0.42 = +12.6 %` of entry — record the arithmetic in `SWEEP_META`.)

24 configs × 18 shards (~2.5 h). **Built-in cross-checks:** `w7_s15_a08_l04`
must reproduce W4 `x_notgt_arm08_stop15` (77 % win / +₹261);
`w7_s15_a06_l06` must reproduce `x_notgt_stop15_arm06_lock06` (81 % / +₹280).

Launch first (no code gate):
```
cd ~/trading-bot/backend
setsid bash scripts/run_refined_sweep_3w7_slxtsl.sh </dev/null >/tmp/sweep3w7_nohup.log 2>&1 & disown
```

---

## QC (verified against current code this session)

**W6 harness change:**
| claim | evidence |
|---|---|
| single structure-level override point | `_reconstruct_exit_current` reads `trade_intent.structure_level` at lines ~1576-1580 |
| `option_type_by_intent` in scope at the `current` branch | dict built line 2389, populated 2500; `for mode in modes` at 2538; `current` branch 2555 |
| `DomainOptionType.CE` valid | `from app.domain.market.models import OptionType as DomainOptionType` line 276; `== DomainOptionType.CE` already used at line 1801 |
| swing data available | `all_underlying_bars: list[Bar]` full OHLC (line 423), loaded whole regardless of shard (line 2870) |
| pivot data, zero new logic | `compute_floor_pivots`/`prior_day_ohlc` imported line 245; `pivots_by_date` initialised ~2390, computed ~2605 for the legs path |
| backward compatible | new kwarg `default=None` → `legacy`/`target_mult`/pivot-leg/`--dates`/non-sharded call sites (3027/3099/3145) unaffected |
| entries unchanged | structure-stop-mode touches exit reconstruction only → all 13 W6 configs = the same 26 `d_pdt_w65` entries |

**W7 (no code):**
| claim | evidence |
|---|---|
| every param in an allowlist | `stop_pct`/`target_pct`/`trail_activation_fraction`/`trail_lock_fraction` ∈ `ORB_PARAM_KEYS` (`api/v1/strategies.py:82-96`); `require_prior_day_trend`/`max_or_range_nifty_points` ∈ CONVICTION/ORB keys |
| `target_pct:1.0` = "no target" works | W4's 10 `x_notgt_*` configs all ran clean at exactly 26 trades |
| arm = +frac×100 % of entry when target_pct=1.0 | `activation_distance = \|target−entry\|·frac = (2E−E)·frac = E·frac`; matches W4 `x_notgt_arm06/08/10/12/14` exactly |
| distinct DB prefix | `s3w7_` ≠ `s3w6_` ≠ `s3w4*` → W6+W7 concurrent-safe; each driver reaps only its own prefix |

**Open caveats (state in the write-up, not blockers):** n=26 → every cell
re-slices the same 26 entries, adjacent cells differ by a handful of trades →
plateau not spike; W6 pivots rarely hit intraday → `s_piv_*` ≈ `s_or_nostop`;
`stop18` = "best so far", not "optimal" (ladder has no turning point yet, a
single trade *can* lose 18 % of premium); **W6 must not launch before
`ruff`/`mypy` + the `or_boundary` byte-identical smoke pass** (W7 has no code
gate).

---

## Analysis (both W6 and W7)

```
~/an_venv/bin/python scripts/analyze_walkforward.py --dir <exact timestamped sweep dir> --configs <csv list>
~/an_venv/bin/python scripts/analyze_refined_batches.py --dir <exact dir> --configs <csv list> --label <x>
```
**W6 judging:** does a swing-anchored structure stop beat `s_or_baseline`
(= the W4 `x_baseline_stop15`: 73 % win, +₹400/lot, PF 5.11)? Plateau across
swing-lookbacks, not a lone spike; IS and OOS both positive; win % not below
60; survives 1.0 %/side slip. Watch `structure_break` exit count/win % in the
exit-mix. Expect `s_piv_*` ≈ `s_or_nostop` (pivots sit ~100-170 pts out,
rarely hit intraday) — the "wide S/R" reference; the **swing** configs carry
the signal. At n=26 with `structure_break` only ~5-6 trades/yr, lookback
differences = a handful of trades → treat as noise unless a clear plateau
appears.

**W7 judging:** extend the stop ladder (does −18 % still beat −15 %, and where
does looser stop stop helping?); find the win% ⇄ ₹ frontier across arm × lock
at each stop; confirm the stop×target interaction at stop 18 (does a wider
target keep adding ₹ the way `stop15+tgt30` did?). Same robust bar — plateau,
IS+OOS, bootstrap, cost sweep. n=26, so paper-trade-to-collect-data, never
size on the backtest.

## Pull before the VM dies (night of 2026-08-31)

```
scp -r -i "<key>" ubuntu@129.159.226.106:~/trading-bot/backend/data/historical/backtest_reports/sweep3w6_structstop_* <local>
scp -r -i "<key>" ubuntu@129.159.226.106:~/trading-bot/backend/data/historical/backtest_reports/sweep3w7_* <local>
```
Then append results to `BACKTEST_LEARNINGS.md` (newest-first, IST) + update the
`project_orb_directional_filter_sweep3_2026_08_29` memory.

## Housekeeping / gotchas learned this session

- `run_backtest.py` never drops its per-`--db-suffix` DB — 2,396 orphans =
  92 GB filled the disk mid-run once. Every driver now reaps its own
  `trading_bot_backtest_<prefix>*` on start / per-config / EXIT. **Give each
  concurrently-running batch a DISTINCT prefix** (`s3w6_`, `s3w7_`, ...) —
  sharing one prefix means one batch's reap kills the other's live DBs.
- Durable fix still owed: `try/finally` DROP (or `--keep-db`) inside
  `run_backtest.py:main()` itself.
- `--all-expiries` glob for analysis: point at the exact timestamped dir, not
  `..._*` (a killed/retried run can leave a second empty dir the glob picks up,
  breaking `--dir`).
- Launch remote sweeps as `setsid bash <script> </dev/null >log 2>&1 & disown`
  with `cd` as its OWN statement (`cd DIR; setsid ...`), never
  `cd DIR && setsid ... &` (the `&` binds the whole `&&`, so a second job in
  the same line runs from `$HOME` and can't find `scripts/...`).
- Kill a runaway sweep by PID/PGID (`kill -- -<pgid>`), never
  `pkill -f run_refined_sweep_...` from inside an ssh one-liner — the pattern
  matches the ssh wrapper's own command line and kills your session.
