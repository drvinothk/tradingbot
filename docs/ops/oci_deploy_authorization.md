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
