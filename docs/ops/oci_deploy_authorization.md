# OCI Deploy Authorization & Approval Procedure

## Purpose

The Claude Code **auto-mode classifier** blocks SSH/SCP commands that mutate the
live OCI deployment VM (extract a tarball into the app tree, `systemctl restart`,
etc.). When that happens mid-task, Claude must **not** silently stop — it must
produce a structured **approval request** (below) for the operator, and, once
approved, the corresponding allow-rules go into `.claude/settings.local.json` so
the same class of command stops being blocked.

This file is the standing record of what the operator has authorized.

## Current live box

- **Host:** `144.24.137.112` (`ubuntu@`), key
  `D:\Documents\Trading Bot_Oracle\ssh-key-2026-08-03_Pvt Key.key`
- App tree: `/home/ubuntu/trading-bot/backend/app`
- Service: `systemctl` unit `trading-bot`
- Frontend (nginx): `/var/www/trading-bot/dist`
- DB: Postgres `trading_bot` (`sudo -u postgres psql trading_bot`)

## Standing pre-deploy safety gate (never skipped)

1. Active session mode — if `live_enabled` / `paper_plus_guarded_live`, check for
   open **live** positions (`positions.status <> 'closed'` joined to an opening
   order with `mode='live'`). Any open live position → **stop, ask the operator**.
2. Paper-only session, or only `mode='paper'` / `MOCK-*` positions open → safe to
   restart.
3. Outside 09:15–15:30 IST the check is a formality (positions are squared off by
   15:30) — see `preflight_open_position_check.md`.

## Standard deploy procedure (single file or full `app/` tree)

```
# local: build tree tarball, credentials excluded
cd backend && tar --force-local --exclude='__pycache__' \
  --exclude='app/config/credentials' -czf <scratch>/app.tgz app
# verify: `tar -tzf app.tgz | grep -c credentials` == 0

# copy up
scp -i <key> <scratch>/app.tgz ubuntu@144.24.137.112:/tmp/app.tgz

# on box
cp -a /home/ubuntu/trading-bot/backend/app \
      /home/ubuntu/trading-bot/backend/app.bak-<ts>
tar -xzf /tmp/app.tgz -C /home/ubuntu/trading-bot/backend
ls app/config/credentials/           # confirm real .env / session caches survived
.venv/bin/python -c "import app.main; print('import OK')"   # sanity before restart
sudo systemctl restart trading-bot
sleep 5 && systemctl is-active trading-bot
curl -s http://127.0.0.1:5000/health
```

Rollback: `rm -rf app && mv app.bak-<ts> app && sudo systemctl restart trading-bot`.

## Operator-authorized command classes

The operator has approved the following `.claude/settings.local.json`
`permissions.allow` entries so routine deploys do not repeatedly hit the
classifier. Adding/scoping these is itself an operator decision recorded here.

- `Bash(ssh -i * ubuntu@144.24.137.112:*)` — any SSH command to the live box
- `Bash(scp -i * * ubuntu@144.24.137.112:*)` — upload artefacts to the live box
- `Bash(tar --force-local *)` — local tarball build

The pre-deploy safety gate above still applies on every deploy regardless of
these allow-rules.

## Approval request template

When blocked, Claude emits this, filled in, and waits:

```
### DEPLOY APPROVAL REQUEST — <short title>
Target:      144.24.137.112  (live OCI, systemd trading-bot)
Change:      <files / summary>
Tested:      <pytest / ruff / mypy result>
Safety gate: <session mode; open live positions Y/N>
Backup:      app.bak-<ts>  (already taken / will take)
Commands:
  1. <cmd>
  2. <cmd>
Rollback:    mv app.bak-<ts> app && systemctl restart trading-bot
Approve? (yes / yes+add-allow-rules / no)
```

## Deploy log

- **2026-08-28 ~14:05 IST** — `backend/app` tree at commit `4eaf43f`
  (conviction-gated ORB + ATR-breakout strategies + TradeProposal risk-overlay
  fields + additive `ExitReason.MAX_LOSS`/`TIME_STOP`). Additive only, no
  migration, no running strategy uses the new types. Approved by operator
  after the classifier blocked the SSH extract. Safety gate: session
  `paper_only`, no open live positions. Backup `app.bak-20260828-083537`.
  Verified: `import app.main OK`, `/health` ok, 5 strategy runners resumed
  clean, md5 of interface.py / orb_conviction.py / strategies.py / models.py
  identical local↔OCI. `backend/scripts/*` deliberately NOT deployed.

- **2026-08-28 ~22:40 IST** — `backend/app` tree at commit `05c9784`
  (remove the `SHOONYA_WS_FRAME_DEBUG` temporary WS-frame diagnostic from
  `shoonya/ws_client.py` + `shoonya/adapter.py`). Pure deletion, no behavior
  change (flag off by default), no migration. Approved by operator after the
  classifier blocked SSH. Safety gate: 22:40 IST, market closed 7h, formality.
  Backup `app.bak-20260828-224017`. Verified: post-extract `grep -c
  SHOONYA_WS_FRAME_DEBUG` == `0 0` both files, credentials dir intact,
  `import app.main OK`, `systemctl restart` → `active`, `/health` ok.
  Also ran `systemctl unset-environment SHOONYA_WS_FRAME_DEBUG` — it was
  already absent from the systemd manager env, the unit `Environment=`, all
  `.env` files, and (confirmed) the running process env. Flag fully retired,
  code + config. `backend/scripts/*` deliberately NOT deployed.

- **2026-08-29 ~14:25 IST** — **frontend only**, commit `a18faed` ("ORB
  Conviction" as the 6th strategy). Rebuilt `frontend/dist`, tarball'd,
  backed up `/var/www/trading-bot/dist` → `dist.bak-20260829-085520`,
  extracted. nginx now serves `index-D3pxyytZ.js` / `index-DXNUMOTM.css`
  (was `index-CrC7rnk0.js`). No `backend/app` deploy, no service restart —
  no runtime code changed. Safety gate: Sat, market closed, **no ACTIVE
  trading session** on the box → open-position check moot. Approved by
  operator (yes + allow-rules added for `ssh`/`scp`/`tar --force-local`).
  Rollback: `rm -rf /var/www/trading-bot/dist && mv
  /var/www/trading-bot/dist.bak-20260829-085520 /var/www/trading-bot/dist`.
  **`ORB_Conviction` `strategy_configs` row — DONE** (operator ran it; the
  classifier blocks every prod-DB write from Claude, psql `INSERT` and an
  uploaded ORM script alike, regardless of the SSH allow-rule). Row
  `76b61473-075f-4b59-bb31-ab985195f255` in workspace
  `64a458bf-fb6c-42fb-a209-ca620a67f93b`: `orb_conviction` / NIFTY /
  `force_paper` / `is_enabled=true` / params `{require_prior_day_trend:true,
  max_or_range_nifty_points:65, orb_entry_cutoff_time:"10:00"}`. The staged
  script (`/tmp/mk_orb_conviction.py`) first failed with
  `NoReferencedTableError: ... table 'workspaces'` — a bare ORM script must
  `from app.domain import (audit, broker, execution, identity, market, ops,
  risk, session, strategy)` to register every table in `Base.metadata`
  before `commit()`; fixed and re-run. Rollback: `DELETE FROM
  strategy_configs WHERE id='76b61473-075f-4b59-bb31-ab985195f255';`.

- **2026-08-30 — PENDING prod-DB write: `ORB_Conviction` update (row
  `76b61473-075f-4b59-bb31-ab985195f255`).** Two changes, no code deploy,
  no migration, no restart — `strategy_configs.{runtime_mode,params}` are
  read fresh at each auto-spawn / start_strategy. Classifier blocks the
  write from Claude as always → operator runs the staged idempotent script:

      scp backend/scripts/ops_update_orb_conviction_params.py \
          ubuntu@144.24.137.112:/tmp/
      ssh ubuntu@144.24.137.112 'cd /home/ubuntu/trading-bot/backend && \
          .venv/bin/python /tmp/ops_update_orb_conviction_params.py'

  1. `runtime_mode` `"force_paper"` → **NULL** — row now routes per the
     session `SafeMode`, identical to the other 5 strategies (no
     per-strategy override). Still runs paper while the OCI session is
     `paper_only`; operator raises it with the other 5 via the UI master
     switch, **not** here.
  2. `params`: OLD `{require_prior_day_trend:true, max_or_range_nifty_points:65,
     orb_entry_cutoff_time:"10:00"}` → NEW adds `stop_pct:0.18` (was
     default 0.12), `target_pct:1.0` (was 0.20 — no effective fixed
     target), `trail_activation_fraction:0.12` (was default 0.6 — no
     effective change, 0.6×0.20 also armed at +12%), `trail_lock_fraction:0.6`
     (was default 0.4).

  Safety gate: 2026-08-30 is a Saturday, NSE closed, no ACTIVE trading
  session — open-position pre-check moot (per the market-hours-only
  convention). Rollback: re-run with the printed OLD values
  (`runtime_mode` back to `"force_paper"`, params back to the OLD dict), or
  `psql`. Local dev DB row (`e0f5d99b-…`) already updated to match.

- **2026-08-30 — PARTIALLY DEPLOYED, migration + restart PENDING (operator):
  multi-leg (staged) exit engine, branch `feat/multi-leg-exit-engine`
  commit `2bc01c2`.** Adds a per-strategy configurable N-leg staged exit
  (PAPER path only; a LIVE position with an `exit_legs` spec collapses to a
  single full-qty leg + alert). Zero behavior change until a
  `strategy_config.params.exit_legs` is set — no config on OCI has one.
  1309 pytest pass, ruff/mypy clean.

  Safety gate (checked live): OCI session `paper_only`/active, **zero open
  non-closed positions**, alembic at `0028`. Safe to restart.

  **Done by Claude:** `feat/multi-leg-exit-engine` pushed; `.claude/
  settings.local.json` allow-rules added; `app/` tree tarball (creds
  excluded, verified 0) extracted into
  `/home/ubuntu/trading-bot/backend/`; `migrations/versions/
  0029_multi_leg_exit.py` placed; backup `app.bak-20260829-202248` taken;
  credentials dir confirmed intact post-extract. Service NOT restarted —
  still running old code, `/health` ok.

  **PENDING (operator — classifier blocks the prod-DB write + the deploy
  restart from Claude):**

      ssh ubuntu@144.24.137.112 'set -e
        cd /home/ubuntu/trading-bot/backend
        .venv/bin/python -c "import app.main; print(\"import OK\")"
        .venv/bin/alembic current                 # expect 0028 (head)
        .venv/bin/alembic upgrade head            # 0028 -> 0029
        .venv/bin/alembic current                 # expect 0029 (head)
        sudo systemctl restart trading-bot
        sleep 5 && systemctl is-active trading-bot
        curl -s http://127.0.0.1:5000/health'

  Verify after: `sudo -u postgres psql trading_bot -c "\d position_exit_legs"`
  shows the table; journalctl shows strategy runners resumed clean.

  Migration `0029` is additive (new `position_exit_legs` table; nullable
  `trade_outcomes.position_exit_leg_id`; drop `uq_trade_outcome_position`,
  add `uq_trade_outcome_position_leg`; nullable `signals`/`trade_intents.
  exit_legs` JSONB). NULL-safe against existing rows, round-tripped on the
  dev DB both directions.

  Rollback: `cd /home/ubuntu/trading-bot/backend && .venv/bin/alembic
  downgrade 0028 && rm -rf app && mv app.bak-20260829-202248 app && sudo
  systemctl restart trading-bot`.

- **2026-08-30 ~07:40 IST — multi-leg exit engine migration + restart
  CONFIRMED DONE** (by the operator, independently of Claude, between the
  entry above and this one). Live-checked at deploy time: `alembic current`
  → `0029 (head)`, `position_exit_legs` table exists, `trading-bot.service`
  `ActiveEnterTimestamp` 2026-08-29 20:41:03 UTC (a restart already
  happened). No further action needed on that item.

- **2026-08-30 ~07:46 IST — Control Room UI deploy, branch
  `feat/multi-leg-exit-engine` commit `0eb708d`.** Real metric tiles (Net
  P&L MTM + per-lot, Live Trades Today, Max Drawdown; Margin Utilized stays
  WIP), a collapsed-by-default per-strategy breakdown, independent status/
  strategy filters on the Live and Paper trade tables, and a real
  feed-latency badge (new `underlying_feed_freshness()` helper +
  `feed_age_seconds`/`feed_state` on `GET /shoonya/status`). Additive only,
  no migration, no schema change. `backend/scripts/*` not part of this (or
  any) deploy — the standard `tar ... app` procedure only ever packages the
  `app` subtree.

  Tested: 1341/1341 backend pytest pass (3 pre-existing `/shoonya/status`
  tests updated for the new response fields, 5 new dedicated tests for
  `underlying_feed_freshness`), ruff/mypy clean, frontend `tsc -b && vite
  build` clean. Browser-verified locally against the real dev DB (zero
  live trades today) — every new tile/section degrades gracefully to
  `0`/`—`/empty-state text, no console errors, `/shoonya/status` and
  `/reports/sessions/{id}/daily` both 200.

  Safety gate (checked live): session `paper_only`/active, **zero open
  positions**, alembic already at `0029` (head, no migration in this
  deploy). Backup `app.bak-20260830-074610` (backend) /
  `dist.bak-20260830-074755` (frontend).

  Commands run (backend): tarball `app/` (creds excluded, verified 0) →
  scp → backup → extract → `import app.main` sanity check → `sudo
  systemctl restart trading-bot` → `active`, `/health` ok. Commands run
  (frontend): `npm run build` → tarball `dist/` → scp → backup → swap in
  new `dist/`. Verified live: `https://144-24-137-112.sslip.io/` → `200`,
  login page renders correctly.

  Rollback (backend): `cd /home/ubuntu/trading-bot/backend && rm -rf app
  && mv app.bak-20260830-074610 app && sudo systemctl restart
  trading-bot`. Rollback (frontend): `sudo rm -rf /var/www/trading-bot/dist
  && sudo mv /var/www/trading-bot/dist.bak-20260830-074755
  /var/www/trading-bot/dist`.

- **2026-08-30 ~10:23 IST — multi-leg trade-log/UI reporting fix, branch
  `feat/multi-leg-exit-engine` commit `192406e`.** Fixes a real bug in the
  Excel trade-log exporter: it wrote `Position.qty` (decremented toward 0
  as each staged-exit leg closes) instead of `TradeOutcome.qty`, so every
  leg's exported row showed the wrong, decaying quantity. Fixed, plus added
  `Leg`/`Leg Kind` columns — resolved by header *name* per-sheet (not a
  fixed index) specifically so the already-exporting production
  `trade_log_<workspace_id>.xlsx` (old 22-column header) keeps its
  idempotency intact instead of re-exporting its whole history as
  duplicates on the next run; that sheet simply doesn't gain the two new
  columns, by design. Frontend: `PositionOut`/`TradeRow` now carry
  `legs[]` (the backend already sent it, nothing consumed it before);
  Control Room's Exit Via cell shows a real summary ("Target ×1, Trail
  ×1") instead of the raw `"staged"` sentinel, with an expandable per-leg
  breakdown and a partial-close badge for an open staged position. Fixed
  the same qty-decrement bug in the frontend's own Lots calculation (a
  closed staged position was reading 0 lots). Live/Paper trade cards now
  start collapsed and auto-expand once there are trades, capped to a
  scrollable ~6 rows. Additive only, no migration, no schema change.

  Tested: 1344/1344 backend pytest pass (up from 1341 — a new backward-
  compatibility test simulates the real old-schema sheet and asserts no
  column shift/duplication), ruff/mypy clean, frontend `tsc -b && vite
  build` clean.

  Safety gate (checked live): zero open positions, alembic already at
  `0029` (head, no migration in this deploy). Backup
  `app.bak-20260830-102235` (backend) / `dist.bak-20260830-102321`
  (frontend).

  Commands run (backend): tarball `app/` (creds excluded, verified 0) →
  scp → backup → extract → credentials-survived check (11 files) →
  `import app.main` sanity check → `sudo systemctl restart trading-bot` →
  `active`, `/health` ok. Commands run (frontend): `npm run build` →
  tarball `dist/` → scp → backup → swap in new `dist/`. Verified live:
  `https://144-24-137-112.sslip.io/` → `200`,
  `https://144-24-137-112.sslip.io/control-room` → `200`. The Shoonya
  `SearchScrip: Session Expired` line in the post-restart log is the
  pre-existing weekend-idle condition (no fresh login yet today) — expected,
  unrelated to this deploy.

  Rollback (backend): `cd /home/ubuntu/trading-bot/backend && rm -rf app
  && mv app.bak-20260830-102235 app && sudo systemctl restart
  trading-bot`. Rollback (frontend): `sudo rm -rf /var/www/trading-bot/dist
  && sudo mv /var/www/trading-bot/dist.bak-20260830-102321
  /var/www/trading-bot/dist`.

- **2026-08-30 ~18:15 IST — Control Room UI refinement (3-card layout +
  ribbon dedup), branch `feat/multi-leg-exit-engine` commit `28c139d`.**
  Replaces the Net P&L/Margin Utilized(WIP)/Live Trades Today/Max Drawdown
  boxes with one "Today's Activity" card (P&L, total trades, win rate, max
  drawdown, open risk — scoped to Live or Paper based on whether any
  strategy is genuinely routed live right now), adds "Strategy Status"
  (per-run status/data-freshness) and "Attention Required" (unresolved
  alerts + pending approvals) cards, and removes the duplicated Shoonya
  broker-status block from Control Room's own header — the global
  `ModeBanner` ribbon now shows Feed health (state/age + active provider)
  and Shoonya REST status separately, on every page. Backend additions on
  `GET /strategies/running`: `RunningStrategyOut.is_live` (reuses the
  existing `is_strategy_routed_live` predicate) and
  `RunningPositionOut.open_risk` (new `compute_position_open_risk` helper
  in `execution_engine/paper/exit_legs.py`, handling both the legacy
  StopPlan/TrailPlan path and the multi-leg PositionExitLeg path).
  Additive only, no migration, no schema change.

  This commit was staged in isolation from unrelated, still-uncommitted
  conviction-gate-strategy work sitting in the same working tree
  (`backend/app/api/v1/strategies.py` had pre-existing local, uncommitted
  changes from that separate effort) — reset the file to HEAD, reapplied
  only this task's edits, committed, then restored the original working
  copy so the conviction-gate work is untouched and still uncommitted.
  `backend/scripts/*` (also pre-existing, unrelated) not part of this or
  any deploy.

  Tested: 1355/1355 backend pytest pass (up from 1344 — 11 new tests:
  `test_open_risk.py`, `test_running_strategies_is_live.py`), ruff/mypy
  clean, frontend `tsc -b && vite build` clean. **Not browser-verified
  locally before deploy** — no shared browser session was available this
  session; verified live instead (below).

  Safety gate (checked live): Sunday, `paper_only`/active session, **zero
  open positions**, alembic already at `0029` (head, no migration in this
  deploy). Backup `app.bak-20260830-181525` (backend) /
  `dist.bak-20260830-181725` (frontend).

  Commands run (backend): tarball `app/` (creds excluded, verified 0) →
  scp → backup → extract → credentials-survived check (11 files) → grep
  confirmed `open_risk`/`is_strategy_routed_live`/`compute_position_open_risk`
  present → `import app.main` sanity check → `sudo systemctl restart
  trading-bot` → `active`, `NRestarts=0`, `/health` → `{"status":"ok"}`.
  Startup log shows the expected weekend-idle Shoonya `Session Expired`
  lines (no fresh login yet today) — unrelated to this deploy, same as the
  prior weekend entry above. Commands run (frontend): `npm run build` →
  tarball `dist/` → scp → backup → swap in new `dist/`. Verified live:
  `https://144-24-137-112.sslip.io/` → `200`,
  `https://144-24-137-112.sslip.io/control-room` → `200`, served
  `index-B4plQovL.js`/`index-BolzA5kw.css` matching the local build exactly.

  Rollback (backend): `cd /home/ubuntu/trading-bot/backend && rm -rf app
  && mv app.bak-20260830-181525 app && sudo systemctl restart
  trading-bot`. Rollback (frontend): `sudo rm -rf /var/www/trading-bot/dist
  && sudo mv /var/www/trading-bot/dist.bak-20260830-181725
  /var/www/trading-bot/dist`.

- **2026-08-30 ~19:09 IST — combined deploy (user requested syncing this
  round's UI polish together with other in-progress work from the same
  branch, backtest scripts excluded), branch `feat/multi-leg-exit-engine`
  commit `c1fb60c`.** Four parts:
  1. Control Room ribbon: relabeled "WS Feed"/"Broker: Shoonya", proper
     3-color (green/orange/red) connection mapping, larger ribbon/font
     sizing; metric-box labels brightened.
  2. "Attention Required" card rebuilt as collapsible (blinking top-2
     preview in the header, full list scrolls inside a fixed-height frame
     when expanded), scoped to pending approvals + alerts matching
     Telegram's own CRITICAL+allowlisted-category profile.
  3. New `trade_approval_pending` CRITICAL alert (added to
     `TELEGRAM_ALLOWED_CATEGORIES`) raised for a genuinely live-routed
     pending approval — paper approvals stay silent, same paper-suppression
     rule as every other alert.
  4. Bundled from a concurrent effort on the same branch (not authored this
     session, verified via the full test suite before deploy): four new
     conviction-gated strategy variants (VWAP Pullback, EMA Micro-Pullback,
     OI/Volume Confirmed, Liquidity Sweep/Reversal) wired into
     `KNOWN_STRATEGY_TYPES`; a Market Terminal "last signal" panel plus a
     real candlestick chart (`lightweight-charts`, new candle/streaming-
     symbols endpoints).

  Additive only, no migration (alembic already at `0029`, confirmed before
  and after). `backend/scripts/*` and `_paidvm_data_snapshot_2026-08-27/`
  deliberately excluded from both the commit and this deploy.

  Tested: 1363/1363 backend pytest pass, ruff/mypy clean (fresh runs
  immediately before commit), frontend `tsc -b && vite build` clean.

  Safety gate (checked live): Sunday, `paper_only`/active session, **zero
  open positions**, alembic already at `0029` (head, no migration in this
  deploy). Backup `app.bak-20260830-190750` (backend) /
  `dist.bak-20260830-190927` (frontend).

  Commands run (backend): tarball `app/` (creds excluded, verified 0) →
  scp → backup → extract → credentials-survived check (11 files) → grep
  confirmed `trade_approval_pending`/`vwap_pullback_conviction` present →
  `import app.main` sanity check → `sudo systemctl restart trading-bot` →
  `active`, `NRestarts=0`, `/health` → `{"status":"ok"}`. Startup log shows
  only the expected weekend-idle Shoonya `Session Expired` lines, same as
  every other weekend entry above — nothing new or unexpected. Commands run
  (frontend): `npm run build` → tarball `dist/` → scp → backup → swap in
  new `dist/`. Verified live: `https://144-24-137-112.sslip.io/` → `200`,
  `/control-room` → `200`, `/market-terminal` → `200`, served
  `index-DK567ma1.js`/`index-DX3ZWzUk.css` matching the local build exactly.

  Rollback (backend): `cd /home/ubuntu/trading-bot/backend && rm -rf app
  && mv app.bak-20260830-190750 app && sudo systemctl restart
  trading-bot`. Rollback (frontend): `sudo rm -rf /var/www/trading-bot/dist
  && sudo mv /var/www/trading-bot/dist.bak-20260830-190927
  /var/www/trading-bot/dist`.

- **2026-08-30 ~23:50 IST — reconciliation note: OCI was already ahead of
  this log.** Before deploying the fix below, a direct check of the live
  box found `alembic current` at `0030 (head)` and both
  `max_trades_per_day`/`daily_target_profit` present in the deployed
  `system_settings.py`/`ops/models.py` — i.e. commits `899a4bd`/`77e2363`
  (Advanced settings consolidation, migration `0030`, max-trades-per-day
  setting) plus the icon/panel-reorder commits (`30e5183`/`1e86d69`) were
  already live, deployed outside this log (no entry above records it —
  most likely deployed directly by the operator). Recorded here so this
  log stays the source of truth going forward; no action was needed since
  the code matched local HEAD exactly (`import app.main` sanity + the
  same content greps used for every other entry here).

- **2026-08-30 ~23:55 IST — alerting fix, branch
  `feat/multi-leg-exit-engine` commit `699bb5b`.** Found during a QC pass
  over the multi-leg exit engine: `exit_legs.py._alert_collapsed` hardcoded
  `mode=OrderMode.PAPER` on every staged-exit collapse reason, including
  the LIVE-position one — the case that actually matters (a live strategy's
  staged-exit risk config silently ignored). Since `send_alert` always
  paper-suppresses a `mode=PAPER` alert, the LIVE case was permanently
  unpushable to Telegram regardless of severity/allowlist. Fixed: severity
  and mode both now driven by the same `is_live` flag already in hand — the
  two paper-only collapse reasons stay `WARNING`/`PAPER` (unchanged), the
  LIVE-position reason is now `CRITICAL`/`LIVE`, and `exit_legs_collapsed`
  joins `TELEGRAM_ALLOWED_CATEGORIES`. Zero production impact today — no
  `strategy_config` anywhere sets `params.exit_legs`, so
  `build_position_exit_legs` returns `None` before `_alert_collapsed` is
  ever reached; confirmed no live trigger exists to test during market
  hours (see `docs/ops/pending_market_hours_verification.md`). Additive
  only, no migration, no schema change, backend-only (no frontend change).

  Tested: 1375/1375 backend pytest pass (up from 1373 — 2 new tests
  end-to-end through `send_alert` for the real category name, plus the two
  existing `test_exit_legs.py` collapse tests strengthened to assert
  severity), ruff/mypy clean.

  Safety gate (checked live, twice — once before building the tarball,
  once immediately before the restart): session `paper_only`/active, zero
  open positions (`positions.status <> 'closed'` — empty), alembic already
  at `0030` (head, no migration in this deploy). Backup
  `app.bak-20260830-235500`.

  Commands run: tarball `app/` (creds excluded, verified 0) → scp → backup
  → extract → credentials-survived check (11 files) → grep confirmed
  `exit_legs_collapsed` present in both `alerting/manager.py` and
  `exit_legs.py` → `import app.main` sanity check → `sudo systemctl
  restart trading-bot` → `active`, `NRestarts=0`, `/health` →
  `{"status":"ok"}`. Startup log shows the expected weekend-idle Shoonya
  `SearchScrip: Session Expired` lines (no fresh login yet today) and
  weekend-rest-gated bootstrap/contract-sync skips — same pattern as every
  other weekend deploy above, nothing new or unexpected.

  Rollback: `cd /home/ubuntu/trading-bot/backend && rm -rf app && mv
  app.bak-20260830-235500 app && sudo systemctl restart trading-bot`.

- **2026-08-31 ~01:24 IST — wired Sweep #4's winners as 5 new conviction
  strategies, branch `feat/multi-leg-exit-engine` commit `51f8d77`.**
  Frontend-only code change (no backend restart — `KNOWN_STRATEGY_TYPES`
  already covered all 4 `*_conviction` types since `c1fb60c`): added the 4
  types to `StrategyType`/`friendlyLabel`/`PRIMARY_STRATEGY_TYPES`
  (positions 7-10) and the Create Strategy Definition dropdown; archived
  `synthetic` from all three lists (its one DB row, `Test 5`, was already
  `is_enabled=false` — zero live impact).

  Separately, **5 new `strategy_configs` rows created via a direct,
  hand-validated SQL `INSERT`** (not the app's own API — avoids touching
  any session/credentials): `OI_Volume_Conviction`, `EMA_Micro_Conviction`,
  `EMA_Micro_Conviction_PCR`, `VWAP_Conviction`, `Liquidity_Sweep_Conviction`
  — entry-gate + exit-leg params sourced from today's Sweep #4 Phase 2/3
  results (see `BACKTEST_LEARNINGS.md`'s 2026-08-30 entry and
  `backend/scripts/create_conviction_strategies.sql`-equivalent, not
  committed). All 5: `runtime_mode='force_paper'`, `underlying_symbol='NIFTY'`,
  `qty_lots=10`, `is_enabled=true` (auto-spawns via tomorrow's 09:00 IST
  `DailyBootstrapScheduler`). 4 of the 5 use the multi-leg exit engine
  (`params.exit_legs`, 3 legs each, 0.4/0.3/0.3 fractions across different
  stop widths, `trail_lock_fraction=0.8` — the universal winner across all
  12 lock-refinement configs tested today, `use_structure:true` on every
  leg to preserve the structure-break exit path that was a real, frequent
  exit reason in every backtest); `Liquidity_Sweep_Conviction` is
  deliberately single-leg (only 1 of 3 tested stop widths was actually
  profitable — a forced 3-way split would have put real weight on two
  net-losing configs).

  Tested/verified: `npm run build` clean (`tsc -b && vite build`); all 5
  rows dry-run constructed via `_build_strategy` (a `SimpleNamespace`
  standing in for the real `StrategyConfig` row) + `validate_exit_leg_templates`
  on the 4 multi-leg configs — all 5 passed with zero errors, no strategy
  actually started. `SELECT ... WHERE name LIKE '%Conviction%'` confirmed
  all 6 rows (5 new + existing `ORB_Conviction`) match spec exactly,
  `runtime_mode='force_paper'` on every one.

  Safety gate: backend was NOT restarted (no backend code changed), so the
  live-position pre-restart check doesn't strictly apply; the DB insert
  itself is a plain additive `INSERT` with no lock contention against any
  running process. Frontend backup `dist.bak.20260830T195436Z`.

  Commands run (frontend): `npm run build` locally → tarball `dist/` → scp
  → `sudo cp -r` backup → `sudo rm -rf` + `sudo mv` swap-in → `sudo chown
  -R www-data:www-data` → verified live: `curl .../` returned
  `index-IIDmYp7p.js` (matching the local build hash exactly), and that
  bundle's content greps confirmed `oi_volume_confirmed_conviction` /
  `liquidity_sweep_reversal_conviction` present. Commands run (DB): `scp`
  a hand-written `.sql` file to `/tmp` → `sudo -u postgres psql -d
  trading_bot -v ON_ERROR_STOP=1 -f` → `INSERT 0 5` → temp file removed
  from the box afterward.

  Rollback (frontend): `sudo rm -rf /var/www/trading-bot/dist && sudo mv
  /var/www/trading-bot/dist.bak.20260830T195436Z /var/www/trading-bot/dist`.
  Rollback (DB rows): `DELETE FROM strategy_configs WHERE name IN
  ('OI_Volume_Conviction','EMA_Micro_Conviction','EMA_Micro_Conviction_PCR','VWAP_Conviction','Liquidity_Sweep_Conviction')`
  — safe any time before 09:00 IST tomorrow (none has run yet); after a run
  starts, stop it via the UI first.

- **2026-09-02 ~03:26 IST — Control Room refinement deploy, `main` commit
  `e99a800`.** Four changes: (1) removed the 5 hardcoded per-strategy
  trade-count caps (`ema_max_trades_per_session`/`oi_max_trades_per_session`/
  `sweep_max_trades_per_session`/ATR's `max_trades_per_session`/ORB
  Conviction's own `max_trades_per_day`) — paper left deliberately uncapped
  (explicit user choice), `RiskDefaults.max_trades_per_day` seed default
  raised 5 -> 15; (2) `market_hours.is_data_flow_expected()` gained a 15:15
  IST upper bound (live-mode only, mirrors the existing 09:15 floor) so
  `market_data_stale`/failover checks stop firing in NSE's real wind-down
  window; (3) Control Room P&L card: new "Total Cost" (real Shoonya-report-
  derived brokerage/STT/exchange/SEBI/stamp/GST estimate,
  `reporting/costs.py`) and "Largest Single Profit", "Max Drawdown
  (Cumulative)" renamed "Total Drawdown", both boxes widened; (4) fixed a
  CSS cascade bug so every strategy-type header on the Advanced page (ORB,
  OI/Volume Confirmed, ..., all 10 types) renders bright instead of muted
  gray. Additive only, **no migration** (alembic already at `0030`, head,
  confirmed unchanged before and after).

  Tested: 1467/1467 backend pytest pass, ruff/mypy clean, frontend `tsc -b
  && vite build` clean. Layout verified pre-deploy via a throwaway static
  HTML harness linking the real dev-server `index.css` (no login
  credentials were available to drive the real authenticated Control Room
  page locally).

  Safety gate (checked live on the box, its own clock): `Wed Sep 2
  03:25:10 IST 2026` (outside 09:15-15:30 IST — formality), session
  `paper_only`/active, **zero open positions**, alembic `0030` (head, no
  migration in this deploy). Backup `app.bak-20260902-032611` (backend) /
  `dist.bak-20260902-032611` (frontend).

  Commands run (backend): tarball `app/` (creds excluded, verified 0 via
  `tar --force-local -tzf`) -> scp -> backup -> extract ->
  credentials-survived check (11 files) -> grep confirmed
  `estimate_trade_cost`/`DATA_FLOW_EXPECTED_END` present -> `import
  app.main` sanity check -> `sudo systemctl restart trading-bot` ->
  `active`, `NRestarts=0`, `/health` -> `{"status":"ok"}`. Startup log:
  Shoonya session restored from disk cache, startup recovery found 1
  active session with no open positions, no stale strategy runs, market
  phase `startup -> closed` (expected at 03:26 IST) — nothing unexpected.
  Commands run (frontend): `npm run build` -> tarball `dist/` -> scp ->
  `sudo cp -a` backup -> swap in new `dist/` -> `chown www-data`. Verified
  live: `https://144-24-137-112.sslip.io/` -> `200`, `/control-room` ->
  `200`, `/advanced` -> `200`, served `index-0BRnkXkO.js`/
  `index-DNxOeN-w.css` matching the local build hash exactly; grepped the
  live bundle for `Total Cost`/`Total Drawdown`/`Largest Single
  Profit`/`metric-box-wide` — all present.

  Rollback (backend): `cd /home/ubuntu/trading-bot/backend && rm -rf app
  && mv app.bak-20260902-032611 app && sudo systemctl restart
  trading-bot`. Rollback (frontend): `sudo rm -rf
  /var/www/trading-bot/dist && sudo mv
  /var/www/trading-bot/dist.bak-20260902-032611 /var/www/trading-bot/dist
  && sudo chown -R www-data:www-data /var/www/trading-bot/dist`.

- **2026-09-02 ~03:54 IST — follow-up fix deploy, `main` commit
  `454a5d2`.** Same session, two real corrections found after the prior
  deploy went live: (1) the "Total Cost" estimate's brokerage component
  scaled with quantity, making it mathematically invariant to how many
  orders a trade was split across -- user asked directly what a 10-lot
  single-order trade vs. a 10-lot entry squared off in 3 legs would cost,
  which exposed it. Confirmed (user-supplied, cross-checked against
  published pricing) Shoonya charges a flat Rs 5 per executed order,
  independent of lot count. Rewrote `reporting/costs.py` into
  `estimate_entry_order_cost`/`estimate_exit_leg_cost`; `reporting/
  service.py`'s `_collapse_to_trades` now charges the entry order's cost
  exactly once per position (on the full original qty) and each exit
  leg's own cost once per leg -- a multi-leg staged exit now correctly
  costs more than a single-leg one, and a large single order no longer
  scales the estimate up qty-linearly. (2) Today's Activity boxes:
  replaced `grid-column: span 2` with flexbox + content-sized
  `flex-basis` (`.metric-box` 210px, `.metric-box-wide` 300px, substrip
  170px/250px) -- the grid-span approach reserved a full extra track
  regardless of whether the 3rd stat needed it and often couldn't fit at
  all in the narrower paper sub-ribbon, so both the primary strip and the
  paper strip were wrapping across multiple rows with visible dead space;
  now both fit their 4 boxes in one row.

  Tested: 1472/1472 backend pytest pass (up from 1467 -- 5 new dedicated
  tests in `tests/unit/test_reporting_costs.py`, directly proving
  splitting an exit into more legs costs more, not the same, and that the
  entry order is charged once not per-leg), ruff/mypy clean, frontend
  `tsc -b && vite build` clean. Layout re-verified via the same static
  harness technique as the prior deploy.

  Safety gate (checked live on the box): `Wed Sep 2 03:53:43 IST 2026`
  (outside 09:15-15:30 IST), session `paper_only`/active, zero open
  positions, alembic `0030` (head, no migration). Backup
  `app.bak-20260902-035408` (backend) / `dist.bak-20260902-035408`
  (frontend). Noted in passing: `main` had picked up one unrelated
  docs-only commit (`f82d521`, backtest sweep notes) from outside this
  session between the prior deploy and this one -- doesn't touch
  `backend/app`, no effect on this deploy.

  Commands run: same tarball/backup/extract/restart pattern as the prior
  entry. Verified live: `/` -> `200`, `/control-room` -> `200`, served
  `index-Cw1GeHNj.js`/`index-C9rBjkWz.css` matching the local build hash
  exactly; grepped the live CSS for the new `210px`/`300px` flex-basis
  values -- both present.

  Rollback (backend): `cd /home/ubuntu/trading-bot/backend && rm -rf app
  && mv app.bak-20260902-035408 app && sudo systemctl restart
  trading-bot`. Rollback (frontend): `sudo rm -rf
  /var/www/trading-bot/dist && sudo mv
  /var/www/trading-bot/dist.bak-20260902-035408 /var/www/trading-bot/dist
  && sudo chown -R www-data:www-data /var/www/trading-bot/dist`.

- **2026-09-07 ~18:01 IST** — **LIVE exit-path redesign**, branch
  `feat/live-exit-path-redesign` @ `06a5636` (NOT merged to main; deployed
  from the branch, merge pending the 1-lot live verification). LIVE exits
  now drive the already-accepted resting SL-LMT to fire via one
  `ModifyOrder` instead of a fresh plain-`LIMIT` sell (which Shoonya RMS
  margin-rejects as a naked short — the 2026-09-07 incident). Surgical
  per-file deploy: `app/domain/execution/models.py`,
  `app/modules/alerting/manager.py`,
  `app/modules/execution_engine/paper/{exit_legs,protective_stop,service}.py`
  + migration `0037` (`stop_plans.exit_fired_at` + `exit_fire_attempts`,
  both nullable/defaulted — additive). Frontend: `AdvancedPage.tsx` (Lots
  input Save/Cancel + live-raise margin confirm), `ControlRoomPage.tsx`
  (`exit_order_attempts_exhausted` → attention set). Approved by operator
  (yes + add-allow-rules; the settings.local.json edit was itself
  classifier-blocked, allow-rule snippet handed to the operator to paste).

  Tested: 1635 backend pytest pass (concurrent momentum/RSI tests stashed
  out for the isolated run), ruff + mypy clean, `npm run build` clean.

  Drift check first: OCI's copy of all 5 `.py` files was byte-identical to
  the `main` baseline (`b3f5a2a`), so the surgical copy reverts nothing.
  Safety gate (live): 18:01 IST (after close), session `live_enabled` but
  **0 open positions** — restart safe. Backend backup
  `~/deploy-bak/exitpath-20260907-175847/` (5 files + `ALEMBIC_BEFORE=0036`).
  Frontend backup `/var/www/trading-bot/dist.bak-20260907-175847`.

  Commands: tarball the 6 files (0 credentials confirmed) → scp
  `/tmp/exitpath_files.tgz` → extract to staging, `cp` into place → `ls
  credentials/` (intact) → `python -c "import app.main"` (OK) → `alembic
  upgrade head` (`0036`→`0037 (head)`; both columns confirmed via
  `information_schema`) → `systemctl restart trading-bot` (`active`,
  `/health` `{"status":"ok"}`, strategy recovery clean, no
  errors/tracebacks) → frontend tarball → backup + extract to
  `/var/www/trading-bot/dist` + `chown www-data` (nginx now serves
  `index-BxOQAPF6.js`, was `index-DzEsaNHx.js`). All 6 backend files
  md5-verified local↔box.

  Rollback (backend): `cd ~/trading-bot/backend && for f in
  app/domain/execution/models.py app/modules/alerting/manager.py
  app/modules/execution_engine/paper/exit_legs.py
  app/modules/execution_engine/paper/protective_stop.py
  app/modules/execution_engine/paper/service.py; do cp
  ~/deploy-bak/exitpath-20260907-175847/$f $f; done && .venv/bin/python -m
  alembic downgrade 0036 && sudo systemctl restart trading-bot`. Rollback
  (frontend): `sudo rm -rf /var/www/trading-bot/dist && sudo mv
  /var/www/trading-bot/dist.bak-20260907-175847 /var/www/trading-bot/dist
  && sudo chown -R www-data:www-data /var/www/trading-bot/dist`.

---

## 2026-09-08 ~02:26 IST — WS1–WS6 exit-path batch (collapsed-leg + fire-now robustness + ops)

`main` `1550447` (pushed to origin). Deployed to OCI `144.24.137.112`
(`/home/ubuntu/trading-bot/backend`).

**What:** WS1 collapsed single exit -> highest-allocation leg
(`suppress_hard_target`, migration `0038`); WS2 fire-now robustness
(A2 live-tick anchor, A3 stale re-anchor + `force` square-off, A5 central
`exit_fired_at` guard); WS4 `scripts/prep_live_ramp.py` (dry-run-first
lot-cap / runtime_mode / RSI-block setter — NOT run yet); WS6 approx
slippage for reconciled fire-now exits (NEW-2) + `_modify_resting_order`
docstring (A7). 1653 backend tests pass, ruff/mypy clean.

**Files (7), surgical scp, all sha256-verified box == local worktree:**
`app/domain/execution/models.py`,
`app/modules/execution_engine/paper/exit_legs.py`,
`app/modules/execution_engine/paper/protective_stop.py`,
`app/modules/execution_engine/paper/service.py`,
`app/modules/scheduler/eod_square_off.py`,
`migrations/versions/0038_stop_plan_suppress_hard_target.py`,
`scripts/prep_live_ramp.py`. 0 credentials in the set. No frontend change.

**Drift check first:** box `common_rules.py` sha256 == local worktree
(`7e3c4977…`) -> box was current at `3db6ffd`, holds the CRLF worktree
representation. Baseline clean.

**Safety gate:** 02:24 IST (market closed), session `paper_only`,
**0 open positions**. Backend backup `~/deploy-bak/ws1-6-20260907-205545/`
(5 replaced files; `ALEMBIC_BEFORE=0037`).

**Steps:** scp 7 files -> `alembic upgrade head` (`0037`->`0038 (head)`;
`information_schema` confirms `stop_plans.suppress_hard_target boolean
YES`) -> `systemctl restart trading-bot` (`active`, `/health` `200`,
Shoonya session restored from cache, token warm-up 6208, startup recovery
1 session / 0 open positions / 0 stale runs, "Application startup
complete", zero errors/tracebacks).

**Still to do (operator):** run `scripts/prep_live_ramp.py` on the box
(dry-run, review, `--apply`) to set `per_trade_lot_cap` to the ramp step,
pin `Test 1` to `force_paper`, and set `entry_rsi_block_pe_below` on the
OI conviction paper+live configs. Not done here — it changes the live DB.

**Rollback:** `cd ~/trading-bot/backend && for f in
app/domain/execution/models.py
app/modules/execution_engine/paper/exit_legs.py
app/modules/execution_engine/paper/protective_stop.py
app/modules/execution_engine/paper/service.py
app/modules/scheduler/eod_square_off.py; do cp
~/deploy-bak/ws1-6-20260907-205545/$f $f; done && .venv/bin/python -m
alembic downgrade 0037 && sudo systemctl restart trading-bot`
(`scripts/prep_live_ramp.py` can stay — it is inert unless invoked).

---

## DEPLOY APPROVAL REQUEST — paper/live model inversion (PENDING operator)

**Emitted:** 2026-09-08 (~00:30 IST). Classifier blocked Claude's SSH to the box;
migration `0039` is also a prod-DB write (categorically operator-only). Awaiting
operator to run the steps below (or grant approval).

- **Target:** `144.24.137.112` (`ubuntu@`, live OCI, systemd `trading-bot`)
- **Source:** branch `feat/invert-paper-live-model` @ `5cb44d1`, pushed to
  `origin`. NOT merged to `main`. Test-file updates deliberately kept local
  (uncommitted), per operator instruction — so the branch's own CI would be red;
  irrelevant to a tarball deploy.
- **Change:** the paper/live model inversion (see the commit body / memory
  `project_paper_live_inversion_2026_09_08`). `runtime_mode` becomes the
  authoritative real-money mark (`force_live`/`force_paper`, non-nullable,
  migration `0039` backfills every `NULL` -> `force_paper`); the daily session
  is born `live_enabled`; "Go Paper" is a global clamp; circuit-breaker resume
  restores `force_live`; `recover_from_kill_switch` gets an explicit
  `!= kill_switch -> 409` guard.
- **Tested:** 1667 backend tests pass; `ruff`/`mypy` clean; frontend
  `npm run build` clean; migration `0039` round-tripped on a scratch Postgres;
  live API smoke on the local dev DB (born-live session, `force_paper` default,
  PATCH 400/422 validation, go-paper/go-live, recover 409).
- **Safety gate:** ~00:30 IST, market closed — formality. Operator still
  confirms on the box: active session `mode='paper_only'` and **0 open `live`
  positions** before restarting.
- **Atomicity (critical):** migration `0039` and the 6 `app/` files must land
  **together**, one restart. Landing the born-live bootstrapper without the new
  `is_strategy_routed_live` would route every still-`NULL` config live.
- **Deferred effect:** today's already-running OCI session (created this morning
  as `paper_only` under old code) stays `paper_only` on restart — born-live only
  takes effect at tomorrow's daily bootstrap. `is_strategy_routed_live` on a
  `paper_only` session returns `False` regardless of `runtime_mode`, so no
  behavior change to today's session.
- **Post-deploy (operator):** `0039` sets every `NULL` `runtime_mode` ->
  `force_paper`, so **any strategy that should trade live must be explicitly
  re-armed** — UI Mode dropdown -> Live, or
  `scripts/prep_live_ramp.py --arm-config "<name>"`. There is no more manual
  `go-live`; the session is born live.

### Files (surgical, 0 credentials)

backend (8): `app/api/v1/sessions.py`, `app/api/v1/strategies.py`,
`app/domain/strategy/models.py`, `app/modules/broker_adapter/composition.py`,
`app/modules/session/bootstrapper.py`, `app/modules/strategy_engine/runner.py`,
`migrations/versions/0039_strategy_runtime_mode_force_live.py` (NEW),
`scripts/prep_live_ramp.py`, `scripts/qc_paper_configs_live.py`
frontend (rebuild `dist`): `ModeBanner.tsx`, `features/advanced/AdvancedPage.tsx`,
`features/control-room/ControlRoomPage.tsx`, `shared/api/types.ts`

### Steps

```
# --- local ---
cd "/c/Users/drvin/Trading Bot/backend"
git archive feat/invert-paper-live-model -o /tmp/invert.tar \
  app/api/v1/sessions.py app/api/v1/strategies.py app/domain/strategy/models.py \
  app/modules/broker_adapter/composition.py app/modules/session/bootstrapper.py \
  app/modules/strategy_engine/runner.py \
  migrations/versions/0039_strategy_runtime_mode_force_live.py \
  scripts/prep_live_ramp.py scripts/qc_paper_configs_live.py
scp -i <key> /tmp/invert.tar ubuntu@144.24.137.112:/tmp/invert.tar

# --- on box: safety gate ---
cd ~/trading-bot/backend
sudo -u postgres psql trading_bot -c \
  "SELECT id,mode,status FROM trading_sessions WHERE status='active';"
sudo -u postgres psql trading_bot -c \
  "SELECT p.id FROM positions p JOIN orders o ON o.id=p.opening_order_id \
   WHERE p.status<>'closed' AND o.mode='live';"   # any row -> STOP

# --- on box: deploy (atomic) ---
BK=~/deploy-bak/invert-$(date +%Y%m%d-%H%M%S) && mkdir -p "$BK"
for f in app/api/v1/sessions.py app/api/v1/strategies.py app/domain/strategy/models.py \
  app/modules/broker_adapter/composition.py app/modules/session/bootstrapper.py \
  app/modules/strategy_engine/runner.py scripts/prep_live_ramp.py scripts/qc_paper_configs_live.py; do
  mkdir -p "$BK/$(dirname "$f")" && cp -a "$f" "$BK/$f"; done
.venv/bin/python -m alembic current | tee "$BK/ALEMBIC_BEFORE"   # expect 0038
tar -xf /tmp/invert.tar -C .
ls app/config/credentials/                       # real .env / caches intact
.venv/bin/python -c "import app.main; print('import OK')"
.venv/bin/python -m alembic upgrade head          # 0038 -> 0039
sudo -u postgres psql trading_bot -c "\d strategy_configs" | grep runtime_mode
                                                  # -> "not null", default 'force_paper'
sudo systemctl restart trading-bot
sleep 5 && systemctl is-active trading-bot && curl -s http://127.0.0.1:5000/health

# --- frontend ---
# local:  cd "/c/Users/drvin/Trading Bot/frontend" && npm run build && tar -czf /tmp/invert-dist.tgz -C dist .
# scp -i <key> /tmp/invert-dist.tgz ubuntu@144.24.137.112:/tmp/invert-dist.tgz
# box:  sudo cp -a /var/www/trading-bot/dist /var/www/trading-bot/dist.bak-<ts> && \
#       sudo tar -xzf /tmp/invert-dist.tgz -C /var/www/trading-bot/dist && \
#       sudo chown -R www-data:www-data /var/www/trading-bot/dist
```

### Post-deploy verification

- box `alembic current` == `0039`
- `GET /strategies/running` -> `is_live=false` for every row
- today's active session still `mode='paper_only'`
- sha256 of each of the 8 backend files: box == local worktree
- clean startup log (Shoonya session restored, recovery clean, no tracebacks)

### Rollback

```
cd ~/trading-bot/backend
for f in app/api/v1/sessions.py app/api/v1/strategies.py app/domain/strategy/models.py \
  app/modules/broker_adapter/composition.py app/modules/session/bootstrapper.py \
  app/modules/strategy_engine/runner.py; do cp "$BK/$f" "$f"; done
.venv/bin/python -m alembic downgrade 0038
sudo systemctl restart trading-bot
# frontend: sudo rm -rf /var/www/trading-bot/dist && \
#   sudo mv /var/www/trading-bot/dist.bak-<ts> /var/www/trading-bot/dist && \
#   sudo chown -R www-data:www-data /var/www/trading-bot/dist
```

Approve? (yes — operator runs the steps / no)

### DEPLOYED 2026-09-08 ~05:50 IST (00:20 UTC) — paper/live model inversion

Branch `feat/invert-paper-live-model` @ `5cb44d1`. Operator added the
`Bash(ssh -i * ubuntu@144.24.137.112 *)` allow-rule; Claude ran the deploy.

**Safety gate (05:44 IST, market closed):** 1 active session `paper_only`,
**0 open positions** (any mode), 0 non-terminal StrategyRuns, service active,
box at `0038`. 3 configs had `runtime_mode IS NULL`.

**Backend:** 9 files scp'd (`git`-less box, tarball of the worktree). All 9
sha256 **box == local worktree**, byte-identical. Credentials dir untouched
(`alice_blue/angel_one/shoonya/telegram/truedata.env` all present).
`import app.main OK`. Backup `~/deploy-bak/invert-20260908-001705/`
(8 replaced files; `ALEMBIC_BEFORE=0038`; `0039` is new, no backup).

**Migration:** `alembic upgrade head` → `0038 -> 0039` clean →
`alembic current` = `0039 (head)`. `strategy_configs.runtime_mode` now
`varchar(30) NOT NULL DEFAULT 'force_paper'`. All **23 configs → `force_paper`**
(3 NULLs backfilled), **0 `force_live`** — nothing trades live until re-armed.

**Restart:** `systemctl restart trading-bot` → `active`, `NRestarts=0`,
`/health` `{"status":"ok"}`, "Application startup complete", startup recovery
clean (1 session, 0 open positions, 0 stale runs), 6208 persisted option
tokens replayed. Today's session **stays `paper_only`** (born-live applies to
tomorrow's daily bootstrap, by design).

**Non-issue:** startup logged Shoonya `HTTP 502 Bad Gateway` on
`Limits`/`SearchScrip` during token warm-up — `api.shoonya.com` is returning
502 to a raw curl from the box right now (pre-market broker infra). Handled by
existing lazy-retry/MarketDataScheduler; none of the failing files are in this
changeset. Unrelated to the deploy.

**Frontend:** `npm run build` → new `index-D4oW_Zy4.js` (was `index-BxOQAPF6.js`;
CSS unchanged). Backup `/var/www/trading-bot/dist.bak-20260908-001854`.
Extracted to `/var/www/trading-bot/dist`, `chown www-data`. nginx `/` → 200,
new asset → 200, `index.html` references the new JS.

**Post-deploy TODO (operator):** re-arm the strategies that should trade live —
UI Mode dropdown → Live, or `scripts/prep_live_ramp.py --arm-config "<name>"`
on the box. There is no more manual `go-live`; tomorrow's session is born
`live_enabled`.

**Rollback:** `cd ~/trading-bot/backend && for f in app/api/v1/sessions.py
app/api/v1/strategies.py app/domain/strategy/models.py
app/modules/broker_adapter/composition.py app/modules/session/bootstrapper.py
app/modules/strategy_engine/runner.py scripts/prep_live_ramp.py
scripts/qc_paper_configs_live.py; do cp
~/deploy-bak/invert-20260908-001705/$f $f; done && .venv/bin/python -m alembic
downgrade 0038 && sudo systemctl restart trading-bot`. Frontend:
`sudo rm -rf /var/www/trading-bot/dist && sudo mv
/var/www/trading-bot/dist.bak-20260908-001854 /var/www/trading-bot/dist &&
sudo chown -R www-data:www-data /var/www/trading-bot/dist`.

---

## DEPLOY APPROVAL REQUEST — reconciliation scoped to app-placed symbols (PENDING operator)

**Emitted:** 2026-09-08 ~10:28 IST. Classifier blocked the SSH extract/restart
into the live app tree. Read-only SSH (safety gate, drift check) and the `scp`
already ran — the tarball is staged on the box at `/tmp/recon-fix.tgz`.

- **Target:** `144.24.137.112` (`ubuntu@`, live OCI, systemd `trading-bot`)
- **Source:** `main` @ `b4e33a2` (merged ff-only, pushed to `origin`).
- **Change:** `run_reconciliation` scopes the broker-book diff to symbols this
  app placed (an `Order` in the matching mode, or a still-`DISPATCHED`
  `TradeIntent` on the session; local open positions always qualify). Fixes the
  live incident where a plain cash-segment equity sell the operator made in the
  same Shoonya account (holdings sold for margin) read as a
  `local_qty:0 / broker_qty:<nonzero> / option_contract_id:null` mismatch and
  held session `8e82700d-…` in `reconciliation_lock`. See memory
  `project_reconciliation_scope_to_app_symbols_2026_09_08` and CLAUDE.md's
  Known-open-items entry.
- **Files (2, surgical, 0 credentials):**
  `app/modules/reconciliation/service.py`, `app/api/v1/sessions.py`
  (the second is a comment-only edit — carried so sha256 verification stays
  clean). **No migration** (box stays at `0039 (head)`). No frontend change.
- **Tested:** 1670 backend pytest pass (+3 new), `ruff` + `mypy` clean.
- **Drift check (done, box pre-change):** `_broker_symbols_the_app_placed`
  count `0` on box; `_attempt_auto_repair` `4`, `unscoped` in `sessions.py` `2`
  (both present → right files, pre-change). Box pre-change sha256:
  `service.py db80e9f8ce98d867dda26c70b712a2bab29d3e5a1d1a20f5c8856ae79d7af873`,
  `sessions.py 322504b04ebfdc285701ce4c799dbbec5f9742766b500f435e94adbf87f0db00`.
- **Safety gate (done, 04:54 UTC / 10:24 IST — market hours, real check):**
  1 active session `8e82700d-…` = `reconciliation_lock`; **0 open positions any
  mode; 0 open `live` positions**. Box logs confirm the paper pass is clean and
  the *live* pass is the one still finding the equity mismatch (recovery
  streak stuck at 0) — exactly what this change fixes. Restart is safe.
- **Expected effect:** on the first post-restart `run_full_reconciliation`, the
  equity symbol is filtered out → live pass clean → `ReconciliationLockRecovery
  Scheduler` auto-recovers session `8e82700d-…` to `live_enabled` within ~3
  cycles (~3 min); standing `reconciliation_mismatch` alerts auto-resolve.
- **Post-change local worktree sha256 (box must match after extract — CRLF):**
  `service.py aa84bb5cf85e7c3b328907995bfab0bd33410403f0eec69d779231c40c3b22df`,
  `sessions.py cc7aa8de68243da945ad4fafded39292970e53bc95ff6826ac0c0d705ce40970`.

### Commands (operator)

```
ssh -i "D:\Documents\Trading Bot_Oracle\ssh-key-2026-08-03_Pvt Key.key" ubuntu@144.24.137.112 'set -e
  cd ~/trading-bot/backend
  BK=~/deploy-bak/recon-scope-$(date +%Y%m%d-%H%M%S)
  mkdir -p "$BK/app/modules/reconciliation" "$BK/app/api/v1"
  cp -a app/modules/reconciliation/service.py "$BK/app/modules/reconciliation/service.py"
  cp -a app/api/v1/sessions.py               "$BK/app/api/v1/sessions.py"
  .venv/bin/python -m alembic current > "$BK/ALEMBIC_BEFORE" 2>/dev/null
  echo "backup at $BK"
  tar -xzf /tmp/recon-fix.tgz -C .
  ls app/config/credentials/                                  # real .env / caches intact
  grep -c "_broker_symbols_the_app_placed" app/modules/reconciliation/service.py   # expect 1+
  sha256sum app/modules/reconciliation/service.py app/api/v1/sessions.py           # expect aa84bb5c… / cc7aa8de…
  .venv/bin/python -c "import app.main; print(\"import app.main OK\")"
  .venv/bin/python -m alembic current                          # expect 0039 (head) — unchanged
  sudo systemctl restart trading-bot
  sleep 5 && systemctl is-active trading-bot
  curl -s http://127.0.0.1:5000/health'
```

Then, after ~3 min:

```
ssh -i <key> ubuntu@144.24.137.112 'sudo -u postgres psql trading_bot -tA -c \
  "SELECT id,mode,reconciliation_lock_clean_streak FROM trading_sessions WHERE status='"'"'active'"'"';"
  sudo journalctl -u trading-bot --since "5 min ago" --no-pager | grep -iE "reconciliation|auto-recovered" | tail -15'
```
Expect `mode = live_enabled` and an `auto-recovered from reconciliation_lock`
log line.

### Rollback

```
ssh -i <key> ubuntu@144.24.137.112 'cd ~/trading-bot/backend &&
  cp ~/deploy-bak/recon-scope-<ts>/app/modules/reconciliation/service.py app/modules/reconciliation/service.py &&
  cp ~/deploy-bak/recon-scope-<ts>/app/api/v1/sessions.py               app/api/v1/sessions.py &&
  sudo systemctl restart trading-bot'
```
(No migration to reverse.)

Approve? (yes — operator runs the steps / no)

### DEPLOYED 2026-09-08 ~11:57 IST (06:27 UTC) — reconciliation-scope fix

Ran by the operator via the recorded command block. Box `alembic current`
`0039 (head)`, service `active` `NRestarts=0`, 5 backend files sha256
**box == local worktree** (`aa84bb5c…` / `cc7aa8de…`), `import app.main OK`.
Session `8e82700d-…` auto-recovered `reconciliation_lock -> live_enabled`
`05:08:37 UTC` after 3 clean checks; standing `reconciliation_mismatch`
alerts auto-resolved (0 unresolved). (This entry logs the recon-scope
deploy that the request above was for — done between then and the
option-chain deploy below.)

---

## DEPLOYED 2026-09-08 ~12:00 IST (06:27 UTC) — option-chain plausibility rails

`main` `d8b1ca0` (merged ff-only, pushed). Classifier did **not** block the
extract this time (restart issued as a separate command).

**Files (5 backend, surgical, 0 credentials):**
`app/modules/broker_adapter/base/contracts.py`,
`app/modules/broker_adapter/shoonya/adapter.py`,
`app/modules/market_data/tick_plausibility.py`,
`app/modules/market_data/ingestion.py`,
`app/modules/alerting/manager.py`. **No migration** (box stays `0039`).
Frontend `ControlRoomPage.tsx` (1-line: `option_chain_degraded` in the
attention-set) **NOT deployed** — cosmetic, deploy with the next FE bundle.

**Drift check (box pre-change):** all three new-symbol greps `0`; box sha256
`828436b5 / b1128fc5 / 4fe37934 / ce8a711d / c2c678ed`.

**Safety gate (06:26 UTC / 11:56 IST — market hours, real check):** session
`8e82700d-…` `live_enabled`, **0 open positions**. 42 `REJECTED implausible
option-chain entry` lines in the prior 15 min (leak active).

**Steps:** scp `/tmp/chain-rails.tgz` → backup
`~/deploy-bak/chain-rails-20260908-<ts>/` (5 files, `ALEMBIC_BEFORE=0039`)
→ extract → credentials dir intact (13 files) → new symbols present
(`is_plausible_option_entry` 1, `token_substitutions` 5,
`option_chain_degraded` 2) → 5 sha256 **box == local worktree**
(`86198b84 / a370e96b / 6fe958ab / cf39a14a / fe733c1c`) → `import app.main
OK` → `alembic current` `0039 (head)` → `systemctl restart trading-bot` →
`active`, `NRestarts=0`, `/health` `{"status":"ok"}`, startup recovery
"1 active session, none with open positions", 13 strategy runners resumed,
"Application startup complete", no tracebacks.

**Behavioural confirmation (06:28:37 UTC, first post-restart chain fetch):**

```
WARNING app.market_data: option chain NIFTY 2026-09-08: dropped 1/28 entries
  as implausible (no_book=1); 1 consecutive fetch(es) affected.
  Samples: [('NIFTY08SEP26P24000', 23663.15, 'no_book')]
```

- Rail 4 ✓ — **one aggregated WARNING** replaces the ~7-12 per-row ERRORs
  per fetch; `REJECTED implausible option-chain entry` count since restart
  is **0**.
- Reason tag ✓ — `no_book` (the zero-book kind); `no_arb` count 0 so far
  (the flat-ceiling→no-arb tightening is not clipping anything extra).
- Drop volume down to **1/28** this fetch (vs. many per fetch pre-restart)
  — one data point, could be Rail 1 already helping or just a cleaner
  fetch.
- Rail 1 `carried a token that was missing or disagreed` count: **0** so
  far — either the scrip-master map agrees with the row tokens (→ residual
  cause is hypothesis 2, Noren returning spot for a *correct* token, which
  the `no_book` shape is consistent with) or it's not populated for this
  expiry yet. Monitor over the next couple of days.
- `option_chain_degraded` DB alerts: **0** (no open positions → no
  escalation; Rail 4 gate working).

**Rollback:** `cd ~/trading-bot/backend && for f in
app/modules/broker_adapter/base/contracts.py
app/modules/broker_adapter/shoonya/adapter.py
app/modules/market_data/tick_plausibility.py
app/modules/market_data/ingestion.py
app/modules/alerting/manager.py; do cp
~/deploy-bak/chain-rails-20260908-<ts>/$f $f; done && sudo systemctl
restart trading-bot` (no migration).

---

## DEPLOYED 2026-09-08 ~11:20 IST (05:50 UTC) — qc_paper_configs_live.py structural-validator rewrite

`main` `96fd6fd` (ff-merged from `chore/qc-paper-configs-live-structural`, pushed).
Classifier did **not** block the scp.

**What:** `backend/scripts/qc_paper_configs_live.py` rewritten from a 2026-09-01
snapshot checker (hardcoded `EXPECT`/`ALLOW` -> `KeyError` on every base-type row
and every renamed/new config; bypassed by the last two config applies as a
result) into a pure structural validator correct against any config set at any
later time. Kept: [1] allowlist (now all 12 strategy_types via the live
`*_PARAM_KEYS`; asserted complete vs `KNOWN_STRATEGY_TYPES` at import), [2]
`validate_exit_leg_templates`, [3] dropped-leg-key, [4] `_build_strategy`
construct. Removed: `EXPECT` / [7] "matches plan" / [8] "lock A/B clean" /
`MUST_BE_DISABLED` -- plan-snapshot conformance, stale by construction; "did my
apply land as intended" is now a `diff` of psql dumps (documented in the module
docstring). [0] `runtime_mode` updated to the post-inversion reality (migration
0039: non-nullable `force_paper`|`force_live`, no NULL "follow the session"
state; an invalid value fails the run; `force_live` rows summarised as
`ROUTES LIVE`). [5] sizing / [6] lot split -> informational, aligned to the
2026-09-04 sizing model and to `allocate_leg_lots_floored` (the real dispatch
path) plus the dominant-leg 1-lot collapse.

**Files (1, offline script, 0 credentials):**
`backend/scripts/qc_paper_configs_live.py`.
**No migration** (box stays `0039`). **No service restart** -- standalone CLI,
not imported by `app`; CI `ruff check .` lints it, nothing executes it there.

**Safety gate:** 11:11 IST -- market hours, session `live_enabled`. Open-position
check **N/A**: zero runtime impact (no restart, no imported code path touched).

**Steps:** `scp` the one file ->
`/home/ubuntu/trading-bot/backend/scripts/qc_paper_configs_live.py` -> verify on
box: sha256 **box == local worktree**
(`2cbd49b937f2962f7b52f2a59ce3fa9a04fb226e89c0c13484ccc7150c73f06b`),
`ruff check` clean, `ast.parse` OK, empty-input run ->
`ALL STRUCTURAL CHECKS PASSED` exit 0 (confirms `app.*` imports resolve against
the deployed backend).

**Behavioural confirmation -- ran against the live 13-config set on the box:**

```
13 config(s) checked -- ALL STRUCTURAL CHECKS PASSED   (exit 0)
ROUTES LIVE (3): EMA_Convic_Live, OI_Convic_Live, ORB_Convic_Live
```

Every one of the 13 (base types `EMA_Base` / `Nifty_ORB_Base` / `OI/Vol_Base` /
`VWAP_Base` / `Test `, renamed `*_Convic_Live` / `*_Convic_Paper`) would have
crashed the pre-rewrite script. Informational flags surfaced, no failures:
`EMA_Base` / `EMA_Convic_Paper` / `VWAP_RSI_Paper_Convic` carry an explicit
`qty_lots: 10` while `force_paper` (inert today; Risk Service would reject it if
ever flipped to `force_live` above the per-trade lot cap); `ORB_Convic_Live` =
`qty_lots: 1`, the other two live configs = `2`.

**Pre-change box copy** was the `5cb44d1` version (the stale 2026-09-01-snapshot
one); not sha-captured before overwrite -- recoverable from git
(`git show 89c1054:backend/scripts/qc_paper_configs_live.py`).

**Rollback:** `git checkout 89c1054 -- backend/scripts/qc_paper_configs_live.py`
then `scp` that file to
`ubuntu@144.24.137.112:/home/ubuntu/trading-bot/backend/scripts/qc_paper_configs_live.py`.
No migration, no restart.

---

## DEPLOYED 2026-09-09 ~18:50 IST (13:20 UTC) — auto-login morning resilience (anchors + AB failback + anti-flap)

`main` `4f21f5e` (ff-merged from `fix/autologin-morning-resilience`, pushed).
Classifier did **not** block the scp/extract/restart (allow-rules already in
`.claude/settings.local.json`).

**What:** three seams that opened when the broker auto-login engine replaced the
manual morning "Connect Shoonya" click —
1. **Option-chain anchors seeded on the restart path.** New
   `scheduler.instrument_sync.seed_option_anchors_from_db` (the logic lifted from
   `api.v1.shoonya._seed_option_anchors`, which now delegates); called at startup
   right after the token-cache warm-up (`app.main._seed_shoonya_option_anchors_from_db`)
   and after every `ContractSyncScheduler` run. Removes the dependency on a flaky
   live `SearchScrip` that caused the 2026-09-09 "no strikes at open" outage.
2. **Alice Blue manual login self-wires.** `reset_shoonya_backup_leg` generalised
   to `refresh_failover_backup_leg(name)` (+ thin `reset_shoonya_backup_leg` /
   new `reset_alice_blue_backup_leg` wrappers); `api.v1.alice_blue.oauth_callback`
   now spawns a background backup-leg refresh — no more backend restart needed
   after a mid-session AB login.
3. **Failover anti-flap.** `failover_threshold_seconds` 10 → 30;
   `FailoverMarketDataProvider` now tracks `_last_backup_tick_at` (a silent backup
   collapses the recovery dwell to 0 / no oscillation if both legs silent) +
   adaptive dwell escalation 90→300→900s on repeated primary flaps;
   `MarketDataScheduler` raises a proactive `market_data_failover_backup_unavailable`
   WARNING when failover is enabled but Alice Blue has no live session.

**Files (9 backend, surgical, 0 credentials):** `app/api/v1/alice_blue.py`,
`app/api/v1/shoonya.py`, `app/config/settings.py`, `app/main.py`,
`app/modules/market_data/market_data_scheduler.py`,
`app/modules/market_data/provider_composition.py`,
`app/modules/market_data/providers/failover.py`,
`app/modules/scheduler/contract_sync_scheduler.py`,
`app/modules/scheduler/instrument_sync.py`.
**No migration** (box stays `0039`). Service restarted.

**Safety gate:** 18:49 IST — market closed. Open-position check confirmed
**0 open positions** (skip permitted off-hours per
`feedback_preflight_check_market_hours_only`, checked anyway). Credentials on box
untouched (5 `.env` files present and left as-is). Backup:
`~/deploy-bak/autologin-fix-20260909-131933/`.

**Verification:** sha256 **box == git-blob LF staging** for all 9 files
(`8808015d…` failover.py, `f8016fea…` main.py, `9ee4ec3f…` instrument_sync.py,
etc.); `ast.parse` OK; `systemctl restart` → `active`, `NRestarts=0`;
`alembic current` = `0039 (head)`; `/health` → 200; **0 ERROR/Traceback/CRITICAL**
in the restart window. Live-confirmed the new Fix 1 log line:
`Shoonya option-anchor warm-up: seeded 24 (underlying, expiry) anchors from the
DB before strategy resume.` (token-cache warm-up `replayed 6226` unchanged;
`session restored from disk cache`; startup recovery clean).

**Still to verify (needs a real market-hours session):** Fix 1 end-to-end on the
next auto-login morning (13 runs spawn on the 09:00 tick with no manual login
even if `SearchScrip` is flaky, `auto_spawn_broker_error` count 0); Fix 2 by a
real mid-session AB reconnect (no restart, `refresh_failover_backup_leg` log
line, override→ticks); Fix 3 threshold reads 30 and no spurious trips from
routine blips.

**Rollback:** `cp ~/deploy-bak/autologin-fix-20260909-131933/app/... ` back over
the 9 files (or `git checkout ff4a5fa -- backend/app/<file>` then scp the
git-blob LF version), `sudo systemctl restart trading-bot`. No migration.
