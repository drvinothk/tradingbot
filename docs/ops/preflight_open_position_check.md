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
