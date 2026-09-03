"""Apply the 3 new "sb6-2leg" staged-exit A/B configs -- Part 1 of the
2026-09-04 staged-exit session (see docs/ops/staged_exit_proportional_
collapse_and_carrier_stop_2026_09_04.md). One new config per strategy,
cloning each base config's CURRENT live `params` and adding a 2-leg
structure-break-tuned staged exit, so paper can A/B the new
persistence/ATR-multiplier values against each base's own unchanged config.

Runs `validate_exit_leg_templates` before writing anything -- the exact
validation `POST /strategies` runs, just via a direct DB session instead of
HTTP (no precedent in this repo for a scripted HTTP client against the
running app; every other config-apply script here, e.g.
`qc_paper_configs_live.py`, imports app code directly against a real DB
session). This is NOT the raw-`psql`-INSERT shortcut the plan doc warns
against -- that path skips validation entirely and would fail-safe to
no-legs at signal time; this script never skips it.

Idempotent: skips (does not overwrite) any name that already exists. Always
run with --dry-run first and read the printed params before applying for
real.

Does NOT touch `EMA_Micro_Conviction`'s own `qty_lots` -- see the plan doc's
"What is pending" for that decision (3, for the eventual minimum-size live
staged-exit test specifically) and why it's deliberately not applied here:
an explicit `params.qty_lots` is NOT mode-aware (wins in both paper and
live, a documented 2026-09-01 gotcha), so setting it now would immediately
shrink this config's *today*, still-`force_paper`, still-iterating paper
position size from its current 10-lot default -- an unrequested side effect
outside this script's actual job.

Usage (run from `backend/`, against whatever DB `DATABASE_URL`/the app's own
settings resolve to -- point this at the live OCI DB deliberately, the same
way any other one-off apply script in this directory does):

    python scripts/apply_sb6_2leg_configs.py --dry-run
    python scripts/apply_sb6_2leg_configs.py
"""

from __future__ import annotations

import argparse
import sys
import uuid

sys.path.insert(0, ".")

from app.core.db.session import session_scope  # noqa: E402
from app.domain.strategy.exit_legs import (  # noqa: E402
    deserialize_exit_leg_templates,
    validate_exit_leg_templates,
)
from app.domain.strategy.models import StrategyConfig, StrategyRuntimeMode  # noqa: E402

# (base config name, strategy_type, new config name) -- strategy_type is
# passed explicitly (not just looked up on the base row) so a base config
# ever getting renamed/retyped can't silently misroute the clone.
#
# The oi_volume_confirmed base config's real live name is "Test " -- a
# trailing space, confirmed live (2026-09-04 dry-run against the OCI DB;
# also visible in CLAUDE.md's own "Test " backtick-quoting). "Test" without
# it does not match any row.
NEW_CONFIGS: list[tuple[str, str, str]] = [
    ("Test 1", "ema_micro_pullback", "Test 1 (sb6-2leg)"),
    ("Test 4", "vwap_pullback", "Test 4 (sb6-2leg)"),
    ("Test ", "oi_volume_confirmed", "Test (sb6-2leg)"),
]

# The delta on top of each base config's own current params -- exactly the
# JSON in the plan doc's Part 1 table. `structure_break_persistence_seconds`/
# `structure_break_atr_multiplier` override whatever the base config has (the
# whole point of the A/B); `exit_legs` is new (the base configs have none).
NEW_PARAM_FIELDS: dict[str, object] = {
    "structure_break_persistence_seconds": 6,
    "structure_break_atr_multiplier": 0.6,
    "exit_legs": [
        {
            "kind": "core",
            "qty_fraction": 0.5,
            "use_structure": True,
            "trail_activation_fraction": 0.5,
            "trail_lock_fraction": 0.5,
        },
        {
            "kind": "runner",
            "qty_fraction": 0.5,
            "use_structure": True,
            "no_target": True,
            "trail_activation_fraction": 0.7,
            "trail_lock_fraction": 0.8,
        },
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would be created; write nothing."
    )
    args = parser.parse_args()

    exit_code = 0
    with session_scope() as db:
        for base_name, strategy_type, new_name in NEW_CONFIGS:
            base = (
                db.query(StrategyConfig)
                .filter(
                    StrategyConfig.name == base_name,
                    StrategyConfig.strategy_type == strategy_type,
                )
                .one_or_none()
            )
            if base is None:
                print(
                    f"SKIP {new_name!r}: base config {base_name!r} "
                    f"(strategy_type={strategy_type}) not found"
                )
                exit_code = 1
                continue

            existing = (
                db.query(StrategyConfig)
                .filter(
                    StrategyConfig.workspace_id == base.workspace_id,
                    StrategyConfig.name == new_name,
                )
                .one_or_none()
            )
            if existing is not None:
                print(f"SKIP {new_name!r}: already exists (id={existing.id})")
                continue

            new_params = dict(base.params or {})
            new_params.update(NEW_PARAM_FIELDS)

            # Same validation POST /strategies runs -- raises ValueError on
            # anything structurally wrong (fraction sum, leg count, etc.)
            # before this script writes a single row.
            templates = deserialize_exit_leg_templates(new_params["exit_legs"])
            assert templates is not None
            validate_exit_leg_templates(templates)

            prefix = "[DRY RUN] " if args.dry_run else ""
            print(
                f"{prefix}CREATE {new_name!r} (strategy_type={strategy_type}, "
                f"workspace_id={base.workspace_id}, underlying_symbol="
                f"{base.underlying_symbol!r}, runtime_mode=force_paper)"
            )
            print(f"  params = {new_params}")

            if args.dry_run:
                continue

            new_config = StrategyConfig(
                id=uuid.uuid4(),
                workspace_id=base.workspace_id,
                name=new_name,
                strategy_type=strategy_type,
                params=new_params,
                is_enabled=True,
                # Deliberately hardcoded, never inherited from the base
                # config -- a brand-new, unbacktested staged-exit variant
                # must never accidentally route live even if a base config
                # (e.g. "Test 1", which currently has runtime_mode=NULL --
                # see project memory) itself follows the session mode.
                runtime_mode=StrategyRuntimeMode.FORCE_PAPER,
                underlying_symbol=base.underlying_symbol,
            )
            db.add(new_config)
            db.flush()
            print(f"  created id={new_config.id}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
