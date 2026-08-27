# A1 box expansion + co-located isolated backtesting

Runbook for expanding the live OCI trading-bot box to 2 OCPU / 12 GB / 100 GB
boot and then running the `backend/scripts/` backtests **on that same box**,
hard-isolated from the live trading app, so the separate paid backtest VM can be
retired.

Related: [`backtest_vm_runbook.md`](backtest_vm_runbook.md) (the standalone paid
VM this replaces), CLAUDE.md "Running it locally".

## Why

The paid backtest VM (`e4-16ocpu-128gb`, `129.159.226.106`, 16 OCPU / 128 GB,
trial credit) must be terminated by the night of 2026-08-31 (credit cutoff
~05:30 IST Sep 1). Rather than pay for a dedicated box, backtests move onto the
live A1 box (`A1_2CPU_12GBRAM_Trading`, `144.24.137.112`).

This **deliberately reverses** the standing rule that backtest/analysis scripts
never run on the production trading box. That rule exists for two reasons —
resource contention and wrong-DB writes — and both are mitigated here (a systemd
cgroup slice + a fully separate Postgres instance). Backtests during market
hours run capped, not blocked (some runs are long and overrun); outside market
hours they get full capacity.

## Free-tier math (Oracle halved Always Free A1 on 2026-06-15)

| Resource | Old Always Free | Current Always Free |
|---|---|---|
| A1 shape | 4 OCPU / 24 GB | **2 OCPU / 12 GB** |
| A1 OCPU-hours / mo | 3,000 | **1,500** |
| A1 GB-hours / mo | 18,000 | **9,000** |
| Block storage | 200 GB total (boot+block, tenancy-wide) | 200 GB (unchanged) |

`A1_2CPU_12GBRAM_Trading` at 2 OCPU / 12 GB is now the **entire** free A1
allowance — no room for a second A1 instance. Running 24/7 ≈ 1,488 OCPU-hrs and
8,928 GB-hrs per month → fits with ~1 % margin. OCPU-hours meter on *allocated*
OCPUs while `RUNNING`, not utilisation, so only stopping the instance creates
slack. Any spill bills to PAYG (free credits absorb it).

Block storage after the paid E4 box is gone: 100 (A1) + 47 + 47 (two E2 micros)
= 194 GB / 200 → fine. Volume sizes are static; 200 GB is a soft cap
(~$0.03/GB-month over, not a cliff). No backup/snapshot policies are attached.

Backtest data is small (~296 MB: `options_1min_past` 240 MB + `underlyings/`
52 MB), so 100 GB boot is generous (real workload need ~40–50 GB).

## Status (2026-08-27)

**Phase 1 — DONE and verified.**
- Boot volume `47 → 100 GB` via `BlockstorageClient.update_boot_volume` (run
  from `E2_1CPU_1GRAM_EmailCamp` `~/a1_venv`), then on the box: block rescan +
  `growpart /dev/sda 1` + `resize2fs /dev/sda1` (online, no reboot). `/` now
  96 GB, 91 GB free.
- Shape `1 OCPU / 6 GB → 2 OCPU / 12 GB` via a capacity-retry loop
  (`a1_resize.py` on `E2_1CPU_1GRAM_EmailCamp`, modelled on the original
  `a1_retry.py`, calling `ComputeClient.update_instance(... shape_config
  ocpus=2 memory_in_gbs=12)`). Caught capacity on the first eligible attempt;
  box rebooted clean in ~7 min; `nproc=2`, `Mem 11Gi`, services active, Shoonya
  session restored from disk cache, no open positions. Cron line archived.

**Remaining** — do after the E4 box is terminated (target 2026-08-31 night):

- Phase 2 — swap `1 GB → 4 GB`; `vm.swappiness=10`.
- Phase 3 — isolation scaffold (`btuser`, separate checkout + venv + `.env`,
  second Postgres cluster on `:5433`).
- Phase 4 — `backtest.slice` cgroup + launcher.
- Phase 5 — time-based CPU-quota flip timers.
- Phase 6 — retire the paid E4 box (confirm data is off it first).

## Why the backtest already partly self-isolates

- **Own database per run.** `run_backtest.py::_backtest_db_name` →
  `trading_bot_backtest_<suffix>`; `_ensure_backtest_database_exists`
  auto-creates it, then `Base.metadata.drop_all`/`create_all` each run (no
  Alembic). It never reads or writes the live `trading_bot` DB.
- The historical SystemAlerts-to-prod-DB leak
  (`strategy_engine/runner.py` `alert_session_factory`) is already fixed —
  `run_backtest.py` passes its own backtest `session_factory` and disables the
  market-hours watchdog.
- It only shares the live DB by reading the same `DB_*` vars from
  `backend/.env` (`DBSettings`). Repoint those and every backtest DB moves.
- No pandas/numpy; ~100–200 MB resident per shard. Bottleneck is Postgres (one
  commit per bar), not CPU — so the real risk of co-location is *database*
  contention with live order/position writes.
- `run_all_strategies_overnight.sh` hardcodes `SHARD_COUNT=10`. On a 2-OCPU box
  drop it to **2**.

## Isolation design

| Layer | Mechanism |
|---|---|
| CPU / RAM / IO | `backtest.slice` cgroup: `CPUQuota` 100 % (market) / 200 % (off-hours) + `CPUWeight=50`, `MemoryMax=6G`, `MemoryHigh=5G`, `IOWeight=50`. |
| Postgres | **Second cluster on `:5433`** (`pg_createcluster 16 backtest`), small `shared_buffers`, its `postgres` process placed **inside `backtest.slice`** so the per-bar commit storm is cgroup-capped and cannot touch the live cluster's WAL/buffers/IO. |
| FS / creds | Dedicated unix user `btuser`, own checkout `/home/btuser/trading-bot` + `.venv` + `.env`, **no read access** to the live `.env` or `app/config/credentials/*` (`/home/ubuntu` is mode 750). |
| Orchestration | Launch only via `systemd-run --slice=backtest.slice …`; never an enabled `.service`. Live `trading-bot.service` untouched. |

Rationale for the numbers:

- `CPUWeight=50` (vs the live app's default 100) gives the live app 2:1 CPU
  priority *whenever the two contend* — this, not a shaved quota, protects live
  latency. `CPUQuota` is just a ceiling: 200 % off-hours is idle-fill, it does
  not reserve anything away from the backend.
- `MemoryMax=6G` is a kill ceiling, not an allocation. Real backtest usage is
  <1 GB; 6 GB is ~6× headroom and still leaves 6 GB for the live app + live
  Postgres + OS (today <3 GB combined). RAM does not speed up backtests.
- `IOWeight` is best-effort — only bites under contention and only if the block
  scheduler exposes proportional weights; the CPU + memory caps carry the
  isolation regardless.

## Phase 2 — swap + swappiness

```bash
sudo swapoff /swapfile && sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile \
  && sudo mkswap /swapfile && sudo swapon /swapfile      # fstab line already present
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf && sudo sysctl --system
```

## Phase 3 — isolation scaffold

1. `sudo adduser --disabled-password btuser` ; `chmod 750 /home/btuser`. Confirm
   `btuser` is in no group that can read `/home/ubuntu/**`, the live
   `backend/.env`, or `app/config/credentials/*`.
2. As `btuser`, copy the repo to **`/home/btuser/trading-bot`** (matches the
   overnight script's `~/trading-bot` + `backend/`-relative path assumptions).
   From `backend/`: `python3 -m venv .venv` (name **must** be `.venv` — the
   script hardcodes `./.venv/bin/python`), then
   `.venv/bin/pip install -e ".[dev]"` (no `windows` extra).
3. Copy `backend/data/historical/` (whole dir, including all of `underlyings/`
   with `INDIA_VIX_alice_index_1min.csv`, and BANKNIFTY) into
   `/home/btuser/trading-bot/backend/data/historical/`.
4. Seed settings (`get_settings()` imports `app.*` on load — a DB-only `.env`
   can raise on other required groups):
   - `cp app/config/environments/local.env.example backend/.env`, then set
     `DB_HOST=127.0.0.1`, `DB_PORT=5433`, `DB_USER=btuser`,
     `DB_PASSWORD=<5433 pw>`, `DB_NAME=trading_bot` (the backtest code appends
     `_backtest_<suffix>`).
   - `for f in app/config/credentials/*.env.example; do cp "$f" "${f%.example}"; done`
     — leave broker creds blank/dummy; backtests never call a broker.
5. Second Postgres cluster:
   ```bash
   sudo pg_createcluster 16 backtest --port 5433 --start
   # /etc/postgresql/16/backtest/postgresql.conf: shared_buffers=256MB, max_connections=50
   # /etc/postgresql/16/backtest/pg_hba.conf:   host all btuser 127.0.0.1/32 scram-sha-256
   sudo -u postgres psql -p 5433 -c "CREATE ROLE btuser LOGIN PASSWORD '…' CREATEDB;"
   # drop-in /etc/systemd/system/postgresql@16-backtest.service.d/slice.conf:
   #   [Service]
   #   Slice=backtest.slice
   sudo systemctl daemon-reload && sudo systemctl restart postgresql@16-backtest
   ```
6. Edit `backend/scripts/run_all_strategies_overnight.sh`: `SHARD_COUNT=10` → `2`.

## Phase 4 — cgroup slice + launcher

`/etc/systemd/system/backtest.slice` (name it exactly this — a
`system-backtest.slice` would nest under `system.slice` and change the
`CPUWeight` maths):

```ini
[Slice]
CPUQuota=100%
CPUWeight=50
MemoryMax=6G
MemoryHigh=5G
IOWeight=50
```

`sudo systemctl daemon-reload`

`/opt/backtest/run_bt.sh` (an admin user runs it with `sudo`; it drops to
`btuser`):

```bash
#!/usr/bin/env bash
set -euo pipefail
TS=$(date +%Y%m%d-%H%M%S)
exec systemd-run --slice=backtest.slice \
  --unit=backtest-${TS} --collect --wait=no \
  --property=Nice=10 \
  --working-directory=/home/btuser/trading-bot/backend \
  --uid=btuser --gid=btuser \
  /bin/bash scripts/run_all_strategies_overnight.sh "$@"
```

Follow a run with `journalctl -fu backtest-<TS>` or `/tmp/backtest_logs/`.

## Phase 5 — time-based CPU-quota flip (explicit UTC — do not change system tz)

System timezone on the box is `Etc/UTC`; India has no DST, so fixed offsets are
safe.

- `backtest-market-open` → 09:00 IST = **03:30 UTC** → `CPUQuota=100%`
- `backtest-market-close` → 15:30 IST = **10:00 UTC** → `CPUQuota=200%`

Each is a `.timer` + `Type=oneshot` `.service`:

```ini
# backtest-market-open.timer  (…-close.timer uses 10:00:00 UTC)
[Timer]
OnCalendar=*-*-* 03:30:00 UTC
Persistent=true

# backtest-market-open.service  (…-close.service uses CPUQuota=200%)
[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl set-property --runtime backtest.slice CPUQuota=100%
```

Plus a boot reconciler `backtest-quota-onboot.service`
(`WantedBy=multi-user.target`) for the case where the box is down across both
times:

```bash
#!/usr/bin/env bash
now=$(date -u +%H%M)
if [[ "$now" > "0330" && "$now" < "1000" ]]; then q=100; else q=200; fi
exec /usr/bin/systemctl set-property --runtime backtest.slice CPUQuota=${q}%
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now backtest-market-open.timer backtest-market-close.timer backtest-quota-onboot.service
```

`--runtime` changes do not survive a reboot; between a reboot and the next timer
fire the slice sits at the static `CPUQuota=100%` in `backtest.slice` — the safe
direction.

## Phase 6 — retire the paid E4 box

1. Only after one full sharded run succeeds on the A1 box **and** live-app
   latency / `free -h` stay stable under it during a market session.
2. Confirm all `backend/data/historical/` + wanted result CSVs are off
   `129.159.226.106`.
3. Terminate `e4-16ocpu-128gb`.

## Verification

1. `systemctl show backtest.slice -p CPUQuota` flips 100 % ↔ 200 % at 03:30 /
   10:00 UTC (test by running the `.service` units manually).
2. `systemctl list-timers | grep backtest` — both listed, next-elapse correct.
3. Launch `run_bt.sh` during market hours: `systemd-cgtop` shows
   `backtest.slice` pinned ≈ 1 core; `systemctl status trading-bot` responsive;
   `free -h` stable; live `/strategies/running` polling normally; the `:5433`
   cluster busy while `:5432` write latency is unaffected.
4. Re-run after 15:30 IST: slice allowed ≈ 2 cores.
5. Backtest rows land only in `trading_bot_backtest_*` on `:5433`; live
   `trading_bot` `system_alerts` / positions row counts unchanged.
6. `sudo reboot` mid-day → slice comes up at 100 %; on-boot reconciler / next
   timer confirms the correct value; `trading-bot` + Shoonya reconnect from
   cache.
