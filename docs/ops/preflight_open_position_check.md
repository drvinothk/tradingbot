# Pre-deploy open-position safety check — when to run it

Before any OCI deploy, the standing rule is: check for open live positions /
active trading sessions before touching production.

**Skip this check entirely outside market hours (09:15-15:30 IST).**
Options/intraday positions are squared off by us at 15:09 IST, or by the
broker's own EOD square-off before 15:30 IST regardless — so outside that
window there is structurally nothing open to check for.

**During market hours (09:15-15:30 IST): always run the check** — query for
open positions / active trading sessions, confirm 0/0 (or get explicit
confirmation of what's open) before deploying.

## How to apply
Before deploying, check current IST time first:
- Within 09:15-15:30 IST -> run the full pre-flight check as before.
- Outside that window -> skip the check, proceed directly to deploy.

Also check the **OCI box's own clock** (`TZ=Asia/Kolkata date`) for the IST
time, not the local dev machine's — they have drifted several hours apart
before, and the box is the one that matters for "is the market open".

## The exact query (run on the OCI box)

Postgres stores these enum values **lowercase** — `'open'`, `'active'`,
`'closed'` — so a query using `'OPEN'`/`'ACTIVE'` silently returns zero rows
and gives a false all-clear (hit once, 2026-08-28). Use:

```sql
-- open positions, grouped by the mode of the session they belong to
select ts.mode, count(*)
from positions p
join trading_sessions ts on ts.id = p.trading_session_id
where p.status = 'open'
group by ts.mode;

-- active trading sessions and their mode
select id, mode from trading_sessions where status = 'active';
```

`sudo -u postgres psql trading_bot -tAc "<query>"` on the box. `positions`
has no `mode`/`created_at`/`updated_at` column — mode comes from the session
(or the entry order's `mode`). A `paper_only` session's open position across
a backend restart is low-risk (paper execution, no real broker order, and
`app.main`'s startup-recovery resumes its `PositionManager` + reconciliation
by design); a live-active session with an open position needs explicit
user confirmation before restarting.
