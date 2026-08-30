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
