# Pending market-hours verification — carried over from 2026-08-30

Written 2026-08-30 ~23:50 IST after the QC pass on Friday-through-Sunday's
updates and the `exit_legs_collapsed` alert-tagging fix. Nothing below
blocks anything — these are items that specifically need a real trading day
(09:15-15:30 IST) to check, or a standing decision that's the operator's to
make, not Claude's.

## 1. ORB_Conviction (`76b61473-075f-4b59-bb31-ab985195f255`) — `runtime_mode` still `force_paper`

Checked directly against the live OCI DB on 2026-08-30 ~23:50 IST:

```
strategy_type   | runtime_mode | params
orb_conviction  | force_paper  | {"stop_pct": 0.18, "target_pct": 1.0, "trail_lock_fraction": 0.6,
                                   "orb_entry_cutoff_time": "10:00", "require_prior_day_trend": true,
                                   "max_or_range_nifty_points": 65, "trail_activation_fraction": 0.12}
```

The **params half** of the 2026-08-29 W7 exit-overlay update (`ops_update_orb_conviction_params.py`)
is applied — confirmed live. The **`runtime_mode` half** (`"force_paper"` → `NULL`, so it
routes per the session `SafeMode` like the other 5 strategies) was **not** applied — it's
still hardcoded to `force_paper`. `docs/ops/oci_deploy_authorization.md`'s own log only ever
recorded this as "PENDING," never a "CONFIRMED DONE" follow-up the way the multi-leg
migration got — this is the direct evidence closing that open question.

**Effect today**: none — the OCI session is `paper_only`, so every strategy runs paper
regardless of `runtime_mode`. **Effect once the master switch is flipped to `live_enabled`**:
ORB_Conviction alone would stay forced to paper while the other 5 strategies go live —
almost certainly not the intent, given the params update was explicitly meant to bring it
in line with the other 5.

**Not fixed by Claude** — this is a `strategy_configs` row write, blocked by the classifier
same as every other prod-DB mutation. If you want it corrected, the fix is one field:

```sql
UPDATE strategy_configs SET runtime_mode = NULL
WHERE id = '76b61473-075f-4b59-bb31-ab985195f255';
```

Rollback: `UPDATE strategy_configs SET runtime_mode = 'force_paper' WHERE id = '76b61473-...'`.

Nothing about this needs market hours to fix (it's a row update, checkable/fixable any
time) — but *confirming* ORB_Conviction actually joins the other 5 live once flipped is
only observable the next time the master switch goes to `live_enabled` during a real
session, which is your call, not something to do by default tomorrow.

## 2. `exit_legs_collapsed` alert fix — no market-hours test needed, noting why

The 2026-08-30 fix (mode/severity tagging on a LIVE staged-exit collapse) is fully covered
by unit/integration tests (`test_exit_legs.py`, `test_alerting_manager.py`) because the
alert path is pure logic gated on the `is_live` flag, not on live market data — there's
nothing time-of-day-dependent to verify. It also has **no natural trigger in production
today or tomorrow**: no `strategy_config` anywhere sets `params.exit_legs`, so
`build_position_exit_legs` returns `None` before `_alert_collapsed` is ever reached. This
item is closed, not carried forward.

## 3. Routine post-deploy monitoring for tomorrow (2026-08-31, 09:15-15:30 IST)

Not specific to this session's changes — standard practice after any deploy, listed here
only because a deploy happened right before market open:

- All 6 strategies (including the 4 new conviction variants and ORB_Conviction) reach
  `scanning` status normally after `DailyBootstrapScheduler`'s 09:00 IST auto-spawn.
- Feed health badge / `underlying_feed_freshness` reads LIVE for NIFTY & BANKNIFTY once
  the market opens (confirms the Control Room 3-card refinements and feed-latency badge
  from 2026-08-30 didn't regress anything).
- `trade_approval_pending` alert (new 2026-08-30) fires correctly if a real approval-required
  live signal comes up — no live-eligible strategy is currently forced-paper except
  ORB_Conviction (see item 1), so this should have a real chance to fire if the master
  switch is live.
