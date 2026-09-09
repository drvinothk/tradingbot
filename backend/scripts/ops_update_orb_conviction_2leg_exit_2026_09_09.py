"""Staged, idempotent update of the live ``ORB_Convic_Live`` /
``ORB_Convic_Paper`` ``strategy_configs.params`` (the OCI ``trading_bot`` DB).

Claude's classifier blocks every prod-DB write (psql and an uploaded ORM
script alike), so the operator runs this on the box:

    cd /home/ubuntu/trading-bot/backend && .venv/bin/python \
        /tmp/ops_update_orb_conviction_2leg_exit_2026_09_09.py

Applies the 2026-09-09 p18b0-b3 backtest decision (see
``backend/scripts/BACKTEST_LEARNINGS.md``, 2026-09-09 ~21:00 IST entry, and
``docs/ops/orb_conviction_config_update_2026_09_09.md``):

  C1  ``max_or_range_nifty_points``  65 -> 70   (OR-width sweep winner, PDT on)
  C2  ``exit_legs``  3-leg core/runner/target 25/25/50 (two uncapped legs)
        -> 2-leg 60/40, BOTH hard-capped:
          core   0.60  stop .20 / target .66 / arm .18 / lock .70   (X70-MID-T66)
          runner 0.40  stop .22 / target .50 / arm .24 / lock .80   (X70-CAP-T50-L80)
  C3  top-level fallback params -> Leg A
        (``stop_pct`` .22->.20, ``target_pct`` .33->.66,
         ``trail_activation_fraction`` .12->.18, ``trail_lock_fraction`` .6->.70)

Touches ``params`` ONLY -- never ``runtime_mode`` / ``runtime_mode_source``.
``ORB_Convic_Live`` keeps its existing ``qty_lots: 10`` verbatim (a separate,
pre-existing item -- it is inert while the row is ``force_paper`` and out of
scope here). ``ORB_Convic_Paper`` has no ``qty_lots`` key and gets none.

Every non-changed key is carried in the target literal explicitly (not a
merge onto the live row) so the printed OLD/NEW diff is the whole story.

Runs the exact ``POST /strategies`` validation --
``deserialize_exit_leg_templates`` + ``validate_exit_leg_templates`` on the
new ``exit_legs``, and ``_build_strategy`` on a throwaway ``StrategyConfig``
carrying the full target params -- BEFORE any write. Aborts on failure,
writes nothing.

Idempotent: a row whose ``params`` already equals its target is skipped.
Re-running after a successful apply is a no-op.

Rollback: restore each row's printed OLD ``params`` with the same assignment
(the verbatim JSON is also in
``docs/ops/orb_conviction_config_update_2026_09_09.md``).
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import date

from app.api.v1.strategies import _build_strategy
from app.core.db.session import SessionLocal

# A bare ORM script must import every domain package so all tables are
# registered in Base.metadata before the session touches an FK
# (NoReferencedTableError on `workspaces` otherwise -- see
# docs/ops/oci_deploy_authorization.md and ops_update_orb_conviction_params.py).
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
from app.domain.strategy.exit_legs import (
    deserialize_exit_leg_templates,
    validate_exit_leg_templates,
)
from app.domain.strategy.models import StrategyConfig

# --- the new 2-leg exit, shared by both rows -------------------------------
# Leg A = X70-MID-T66 (profit engine, wider +66% ceiling, looser trail).
# Leg B = X70-CAP-T50-L80 (banks faster/harder, +50% ceiling, lock .80).
# Both arm the trail at ~+12% of entry (target_pct x trail_activation_fraction:
# 0.66 x 0.18 == 0.50 x 0.24 == 0.12) -- the per-leg arm fractions differ on
# purpose, to equalise the arm point across the two different targets.
# `use_structure: true` kept on both (parity with every sibling conviction
# config; adds an OR-structure-break exit on top of stop/target/trail).
_EXIT_LEGS: list[dict[str, object]] = [
    {
        "kind": "core",
        "qty_fraction": 0.60,
        "stop_pct": 0.20,
        "target_pct": 0.66,
        "trail_activation_fraction": 0.18,
        "trail_lock_fraction": 0.70,
        "use_structure": True,
    },
    {
        "kind": "runner",
        "qty_fraction": 0.40,
        "stop_pct": 0.22,
        "target_pct": 0.50,
        "trail_activation_fraction": 0.24,
        "trail_lock_fraction": 0.80,
        "use_structure": True,
    },
]

# --- full target params per row (explicit, not a merge) -------------------
_COMMON: dict[str, object] = {
    # C3 -- top-level fallback == Leg A (governs the malformed-/no-exit_legs
    # single-exit path and Risk pre-trade analytics; the 1-lot collapse reads
    # Leg A's own leg params via pick_collapsed_exit_leg, same net effect).
    "stop_pct": 0.20,
    "target_pct": 0.66,
    "trail_activation_fraction": 0.18,
    "trail_lock_fraction": 0.70,
    # C1
    "max_or_range_nifty_points": 70,
    # unchanged entry gate
    "orb_entry_cutoff_time": "10:15",
    "require_prior_day_trend": True,
    "entry_rsi_block_ce_above": 75,
    "entry_rsi_block_pe_below": 25,
    "entry_require_confirm_bar": True,
    # C2
    "exit_legs": _EXIT_LEGS,
}

ORB_CONVIC_LIVE_ID = "7329fdf0-aef6-4b11-bec2-385d1c3a5c81"
ORB_CONVIC_PAPER_ID = "76b61473-075f-4b59-bb31-ab985195f255"

# ORB_Convic_Live keeps its existing explicit qty_lots verbatim; _Paper has none.
TARGET_PARAMS: dict[str, dict[str, object]] = {
    ORB_CONVIC_LIVE_ID: {"qty_lots": 10, **_COMMON},
    ORB_CONVIC_PAPER_ID: dict(_COMMON),
}


def _preflight_validate() -> None:
    """The exact create/update-strategy validation, before touching the DB."""
    templates = deserialize_exit_leg_templates(_EXIT_LEGS)
    assert templates is not None, "exit_legs deserialised to None"
    validate_exit_leg_templates(templates)  # raises ValueError on any problem

    for row_id, params in TARGET_PARAMS.items():
        cfg = StrategyConfig(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            name=f"preflight-{row_id}",
            strategy_type="orb_conviction",
            params=copy.deepcopy(params),
        )
        strat = _build_strategy(cfg, uuid.uuid4(), date.today())
        print(f"  preflight {row_id}: builds {type(strat).__name__} OK")


def main() -> None:
    _preflight_validate()

    db = SessionLocal()
    try:
        changed = 0
        for row_id, target in TARGET_PARAMS.items():
            row = db.get(StrategyConfig, row_id)
            if row is None:
                raise SystemExit(f"row {row_id} not found -- wrong DB or id")
            if row.strategy_type != "orb_conviction":
                raise SystemExit(
                    f"row {row_id} is {row.strategy_type!r}, expected 'orb_conviction'"
                )

            old_params = dict(row.params or {})
            print("=" * 88)
            print(f"row        : {row.id}  {row.name}  ({row.strategy_type})")
            print(f"runtime    : {row.runtime_mode} / {row.runtime_mode_source}  (unchanged)")
            print(f"OLD params : {json.dumps(old_params, sort_keys=True)}")
            print(f"NEW params : {json.dumps(target, sort_keys=True)}")

            if old_params == target:
                print("  -> no change (already at target)")
                continue

            row.params = copy.deepcopy(target)
            changed += 1
            print("  -> staged")

        if changed == 0:
            print("=" * 88)
            print("nothing to do -- both rows already at target")
            return

        db.commit()
        print("=" * 88)
        print(f"committed {changed} row(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
