"""Staged, idempotent update of the live `ORB_Conviction` strategy_configs row.

Claude's classifier blocks every prod-DB write (psql and an uploaded ORM
script alike), so the operator runs this on the box:

    cd /home/ubuntu/trading-bot/backend && .venv/bin/python \
        /tmp/ops_update_orb_conviction_params.py

Target row: 76b61473-075f-4b59-bb31-ab985195f255
(workspace 64a458bf-fb6c-42fb-a209-ca620a67f93b, orb_conviction / NIFTY).

Two changes:

1. `runtime_mode` "force_paper" -> NULL. The row now routes per the
   session's SafeMode exactly like the other 5 strategies -- no
   per-strategy override. It still runs paper while the session is
   `paper_only`; the operator raises it (with the other 5) via the UI
   master switch, not here.

2. `params` -> the sweep-#3 W7 balanced exit overlay on top of the
   unchanged d_pdt_w65 entry gate:

       stop_pct 0.12 -> 0.18   (looser premium stop)
       target_pct 0.20 -> 1.0  (no effective fixed target; trail carries it)
       trail_activation_fraction 0.6 -> 0.12  (no effective change: 0.6 x
                                               0.20 armed at +12% too)
       trail_lock_fraction 0.4 -> 0.6  (bank more of the run-up; spike-robust
                                        vs 0.8)

   Entry params unchanged: require_prior_day_trend, max_or_range_nifty_points
   65, orb_entry_cutoff_time "10:00".

Idempotent: re-running is a no-op once the row already matches.
Rollback: restore the printed OLD values with the same UPDATE
(runtime_mode back to "force_paper", params back to the OLD dict).
"""

from __future__ import annotations

import json

from app.core.db.session import SessionLocal

# A bare ORM script must import every domain package so all tables are
# registered in Base.metadata before the session touches an FK
# (NoReferencedTableError on `workspaces` otherwise -- see
# docs/ops/oci_deploy_authorization.md).
from app.domain import (  # noqa: F401
    audit,
    broker,
    execution,
    identity,
    market,
    ops,
    risk,
    session,
    strategy,
)
from app.domain.strategy.models import StrategyConfig, StrategyRuntimeMode

ROW_ID = "76b61473-075f-4b59-bb31-ab985195f255"

TARGET_RUNTIME_MODE: StrategyRuntimeMode | None = None

TARGET_PARAMS = {
    "require_prior_day_trend": True,
    "max_or_range_nifty_points": 65,
    "orb_entry_cutoff_time": "10:00",
    "stop_pct": 0.18,
    "target_pct": 1.0,
    "trail_activation_fraction": 0.12,
    "trail_lock_fraction": 0.6,
}


def main() -> None:
    db = SessionLocal()
    try:
        row = db.get(StrategyConfig, ROW_ID)
        if row is None:
            raise SystemExit(f"row {ROW_ID} not found -- wrong DB or id")

        old_params = dict(row.params or {})
        old_mode = row.runtime_mode
        print("row              :", row.id, row.name, row.strategy_type)
        print("OLD runtime_mode :", old_mode)
        print("NEW runtime_mode :", TARGET_RUNTIME_MODE)
        print("OLD params       :", json.dumps(old_params, sort_keys=True))
        print("NEW params       :", json.dumps(TARGET_PARAMS, sort_keys=True))

        if old_params == TARGET_PARAMS and old_mode == TARGET_RUNTIME_MODE:
            print("no change -- already at target")
            return

        row.runtime_mode = TARGET_RUNTIME_MODE
        row.params = dict(TARGET_PARAMS)
        db.commit()
        print("committed.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
