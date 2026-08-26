# Running backtests on the dedicated backtest VM

How to access the dedicated backtest-only OCI VM and run `run_backtest.py`
against it — separate from the live trading deployment (see CLAUDE.md's
"Running it locally" for the live app; this doc is backtesting only).

## VM identity (current instance)

- Display name: `e4-16ocpu-128gb`, shape `VM.Standard.E4.Flex`, 16 OCPU /
  128GB RAM (32 vCPU threads, ~125GB RAM live), 100GB boot volume, Ubuntu
  24.04 x86_64, region ap-hyderabad-1 AD-1.
- Public IP: `129.159.226.106`
- **Paid instance, billed against OCI trial credit — hard cutoff ~05:30 IST
  2026-09-01, target terminate by the night of 2026-08-31.** Confirm this VM
  still exists / check for a replacement before following this doc verbatim
  in a later session — see `project_backtest_vm_e4_16ocpu_128gb_2026_08_26`
  in memory for the full provisioning history if a new instance is ever
  needed.

## SSH access

Same keypair as the live trading-bot boxes (see `reference_oci_ssh_access`
in memory):
```bash
ssh -i "D:\Documents\Trading Bot_Oracle\ssh-key-2026-08-03_Pvt Key.key" ubuntu@129.159.226.106
```
User `ubuntu`, not `opc`/`root`. From a sandboxed session, copy the key
somewhere writable and `chmod 600` it first (Windows source permissions
aren't usable as-is).

## One-time environment (already set up on the current instance)

- Postgres installed natively (`apt install postgresql`, not Docker — this
  is a single-purpose box). A `trading_bot` role with `CREATEDB` exists.
  `run_backtest.py` creates its own isolated `<DB_NAME>_backtest_<suffix>`
  databases on demand (`_ensure_backtest_database_exists` /
  `Base.metadata.create_all`) — no Alembic needed.
- Repo cloned at `~/trading-bot`, venv at `~/trading-bot/backend/.venv`
  (`pip install -e ".[dev]"`, no `windows` extra).

## Deploying code

No `rsync` on Windows Git Bash — use `tar | ssh`:
```bash
cd "/c/Users/drvin/Trading Bot"
tar --exclude='.venv' --exclude='.venv-backtest' --exclude='.venv-truedata' \
    --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.mypy_cache' \
    --exclude='.ruff_cache' --exclude='.coverage' --exclude='data' \
    --exclude='.git' --exclude='.env' \
    -czf - backend | ssh -i "<key>" ubuntu@129.159.226.106 \
    "mkdir -p ~/trading-bot && tar xzf - -C ~/trading-bot"
```
**This repo has 3 local venvs** (`.venv`, `.venv-backtest`, `.venv-truedata`)
— check `ls -la backend | grep venv` before excluding; missing one silently
balloons the transfer by hundreds of MB (caught previously via remote `du -sh`
showing an implausible size mid-transfer, before `scripts/`/`pyproject.toml`
had even landed).

## Syncing historical data

```bash
scp -i "<key>" -r "backend/data/historical/options_1min_past/NIFTY" \
    ubuntu@129.159.226.106:~/trading-bot/backend/data/historical/options_1min_past/
scp -i "<key>" -r "backend/data/historical/underlyings" \
    ubuntu@129.159.226.106:~/trading-bot/backend/data/historical/
```
**Sync the whole `underlyings/` directory, not individual files per
strategy's stated needs** — cherry-picking (e.g. only the one underlying's
`_alice_index_1min.csv`) silently dropped `INDIA_VIX_alice_index_1min.csv`
once, leaving every `vix_entry`/`vix_exit` diagnostic column blank across a
full report until caught by a direct question about it.

## Running a backtest

**Quick single-strategy/single-day smoke test** (seconds):
```bash
cd ~/trading-bot/backend
./.venv/bin/python scripts/run_backtest.py --strategy orb --underlying NIFTY \
    --from 2026-08-01 --to 2026-08-01 --options-subdir options_1min_past \
    --underlying-source alice_index --exit-mode legacy
```
**Always run this smoke test (one expiry, seconds each) for every strategy
before committing to a full/overnight run.** It's what caught the
`futures_proxy` filename-mismatch bug (see the lessons-learned section
below) before it could waste hours of overnight compute — 4/5 strategies
passed immediately, the 5th failed with a clear "missing file" error instead
of silently producing zero trades.

**Full sharded run, all expiries, all 6 exit modes, survives the laptop
closing**: see `backend/scripts/run_all_strategies_overnight.sh` (deployed
to the VM, not committed — matches the "backtest scripts stay local/
one-off" convention). Launch fully detached so it survives both the SSH
session ending and the local machine sleeping:
```bash
scp -i "<key>" backend/scripts/run_all_strategies_overnight.sh \
    ubuntu@129.159.226.106:~/trading-bot/backend/scripts/
ssh -i "<key>" ubuntu@129.159.226.106 \
    "cd ~/trading-bot/backend && chmod +x scripts/run_all_strategies_overnight.sh && \
     setsid nohup ./scripts/run_all_strategies_overnight.sh > /tmp/backtest_driver.log 2>&1 < /dev/null & disown -a"
```
Progress: `~/trading-bot/backtest_status.log` on the VM (timestamped,
per-strategy). This VM's 32 vCPU + native same-box Postgres made 10-way
sharding safe (`max_connections=100`, load stayed under 5 throughout) and
dramatically faster than any local-PC/Docker-Postgres run — a full
5-strategy × 6-mode × 52-expiry NIFTY sweep took 23 minutes here, vs. an
originally-estimated 5-7 hours locally.

**Single-day replay against real, very recent data** (e.g. validating
today's actual paper trades — no pre-existing archive covers a still-live
expiry): pull fresh 1-min bars via Shoonya TPSeries instead of the
TrueData/Alice Blue archive. This needs a live authenticated Shoonya
session, which only the *production* box has cached — run the one-off pull
script there (not on this backtest VM), then copy the results here or to
wherever `run_backtest.py` runs. See `fetch_today_replay_data.py` in
`backend/scripts/` (kept locally, not deployed anywhere long-term) and
`project_backtest_vs_live_paper_validation_2026_08_26` in memory for the
worked example (token lookup via `option_contracts.broker_token` on
production's own DB, TPSeries fetch, exact file layout
`run_backtest.py` expects).

## Pulling results back

```bash
scp -i "<key>" ubuntu@129.159.226.106:"~/trading-bot/backend/data/historical/backtest_reports/*_summary.txt" \
    "backend/data/historical/backtest_reports/"
scp -i "<key>" ubuntu@129.159.226.106:"~/trading-bot/backend/data/historical/backtest_reports/*_NIFTY_trades_*.csv" \
    "backend/data/historical/backtest_reports/"
```
(Merged, non-shard CSVs only — shard-level files are intermediate.)

## Conventions

- **Backtest/analysis scripts never live on any deployed box long-term** —
  deploy via scp, run, then delete from the remote (`rm scripts/<script>.py`).
  They stay local (untracked) in this repo's `backend/scripts/` afterward —
  never synced to the *production* trading-bot box at all (a separate rule
  from this backtest VM, which exists solely to run them).
- This backtest VM has no broker credentials and shouldn't need any — any
  workflow needing a live broker session (fetching very recent data not yet
  in the historical archive) goes through the *production* box's cached
  session instead, per the single-day-replay note above.
