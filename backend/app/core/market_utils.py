"""Day-type classification for strategies/risk checks that behave
differently on expiry day. Deliberately separate from `app.core.clock`
(pure time-zone conversion) — this module is about market calendar
semantics, clock.py is about wall-clock/timestamp conversion.
"""

from __future__ import annotations

import uuid
from datetime import date

# Both NIFTY and BANKNIFTY's real weekly expiry is Tuesday, confirmed from
# live Shoonya scrip-master data (18-AUG-2026, 25-AUG-2026 are both
# Tuesdays — see CLAUDE.md's 2026-08-12 note). date.weekday(): Monday=0 ...
# Tuesday=1. Same constant for both underlyings today; instrument_id is
# accepted (not yet used) so a future per-instrument/per-underlying expiry
# calendar doesn't need a signature change.
WEEKLY_EXPIRY_WEEKDAY = 1

# TODO: ignores exchange holidays -- a Tuesday that's actually an NSE
# holiday will be misclassified as an expiry day until a real holiday
# calendar is integrated.


def is_expiry_day(instrument_id: uuid.UUID, check_date: date) -> bool:
    return check_date.weekday() == WEEKLY_EXPIRY_WEEKDAY


# TODO: same gap as WEEKLY_EXPIRY_WEEKDAY above -- ignores exchange holidays.
# An NSE holiday landing on a weekday is misclassified as a trading day until
# a real holiday calendar is integrated (DB table or broker/exchange API).
def is_trading_day(check_date: date) -> bool:
    """Monday-Friday only. Ops-Hardening Phase 6 (Auto-Spawner): the guard
    that keeps the daily strategy auto-spawner from hammering the DB/broker
    on weekends.
    """
    return check_date.weekday() < 5
