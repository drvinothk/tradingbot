"""One-off: disable the 3 stale base configs that now duplicate their own
2026-09-04 "sb6-2leg" variant's `strategy_type` (see docs/ops/staged_exit_
proportional_collapse_and_carrier_stop_2026_09_04.md and this session's
apply_sb6_2leg_configs.py). The Advanced page groups configs by strategy_type
and, until it gets a Name column (deferred, tracked as a pending item), two
same-type configs render as identical unlabeled rows -- these 3 pairs are the
only type-groups with more than one enabled config today.

Disables (`is_enabled=False`) exactly:
  ("Test ", "oi_volume_confirmed")   -- NOTE trailing space, confirmed live
  ("Test 1", "ema_micro_pullback")
  ("Test 4", "vwap_pullback")

and keeps their "(sb6-2leg)" siblings untouched/enabled. Not a raw SQL
UPDATE -- mirrors exactly what `POST /strategies/{id}/power {is_enabled:
false}` does (api/v1/strategies.py's set_strategy_power/_stop_active_run):
sets is_enabled, stops any still-active StrategyRun (status=STOPPED,
stopped_at, sleep-inhibitor release), and writes the same two audit_events
event_types (strategy_config.updated, strategy_run.stopped) with
actor_type=SYSTEM/actor_id=None -- the same actor shape runner.py's own
EOD-scanning-stop path already uses to stop a run from outside its own loop
iteration (confirmed via that file's ~line 224-239), not a novel pattern.

The runner thread's own loop (strategy_engine/runner.py: StrategyRunner
._loop) polls its StrategyRun.status at the top of every ~30s cycle and
self-terminates the moment it sees STOPPED -- that's the authoritative stop
signal; writing it here from a separate DB session is a correct, complete
stop, not a partial one. Zero open positions confirmed on Test 1/Test 4
before running this (query in the plan doc), so there is nothing for
PositionManager to keep managing either way.

Usage (run from `backend/`, against whatever DB the app's own settings
resolve to):

    python scripts/disable_stale_duplicate_configs.py --dry-run
    python scripts/disable_stale_duplicate_configs.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

sys.path.insert(0, ".")

from app.core.db.session import session_scope  # noqa: E402
from app.core.sleep_inhibitor import get_sleep_inhibitor  # noqa: E402
from app.domain.audit.models import ActorType, EventCategory  # noqa: E402

# None of these four are referenced directly -- imported so SQLAlchemy's
# mapper sees every table StrategyRun/AuditEvent's own FKs point at
# (workspaces, users, broker_accounts, instruments, trading_sessions) before
# the first flush. Same FK-registration need apply_sb6_2leg_configs.py hit
# for Workspace alone; StrategyRun/AuditEvent between them need the rest too
# (found live -- the first run here crashed on `instruments` specifically).
from app.domain.identity.models import BrokerAccount, User, Workspace  # noqa: E402,F401
from app.domain.market.models import Instrument  # noqa: E402,F401
from app.domain.session.models import TradingSession  # noqa: E402,F401
from app.domain.strategy.models import StrategyConfig, StrategyRun, StrategyRunStatus  # noqa: E402
from app.modules.audit_service.service import record_event  # noqa: E402

# (name, strategy_type) -- exact match, no LIKE/trim, so a name drift can
# never silently hit the wrong row or no row at all.
TARGETS: list[tuple[str, str]] = [
    ("Test ", "oi_volume_confirmed"),
    ("Test 1", "ema_micro_pullback"),
    ("Test 4", "vwap_pullback"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would change; write nothing."
    )
    args = parser.parse_args()

    exit_code = 0
    with session_scope() as db:
        for name, strategy_type in TARGETS:
            config = (
                db.query(StrategyConfig)
                .filter(
                    StrategyConfig.name == name,
                    StrategyConfig.strategy_type == strategy_type,
                )
                .one_or_none()
            )
            if config is None:
                print(f"SKIP {name!r} (strategy_type={strategy_type}): not found")
                exit_code = 1
                continue

            prefix = "[DRY RUN] " if args.dry_run else ""

            if config.is_enabled:
                print(f"{prefix}DISABLE {name!r} (id={config.id}, strategy_type={strategy_type})")
                if not args.dry_run:
                    config.is_enabled = False
                    db.flush()
                    record_event(
                        db,
                        workspace_id=config.workspace_id,
                        actor_type=ActorType.SYSTEM,
                        actor_id=None,
                        event_category=EventCategory.STRATEGY_STATE_CHANGE,
                        event_type="strategy_config.updated",
                        entity_type="strategy_config",
                        entity_id=config.id,
                        strategy_config_id=config.id,
                        payload={"is_enabled": False},
                    )
            else:
                print(f"{prefix}already disabled {name!r} (id={config.id})")

            run = (
                db.query(StrategyRun)
                .filter(
                    StrategyRun.strategy_config_id == config.id,
                    StrategyRun.status != StrategyRunStatus.STOPPED,
                )
                .order_by(StrategyRun.started_at.desc())
                .first()
            )
            if run is None:
                print(f"  no active run for {name!r}")
                continue

            print(f"{prefix}STOP active run {run.id} (status={run.status}) for {name!r}")
            if args.dry_run:
                continue

            run.status = StrategyRunStatus.STOPPED
            run.stopped_at = datetime.now(UTC)
            db.add(run)
            db.flush()

            get_sleep_inhibitor().release(f"strategy_run:{run.id}")

            record_event(
                db,
                workspace_id=config.workspace_id,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                event_category=EventCategory.STRATEGY_STATE_CHANGE,
                event_type="strategy_run.stopped",
                entity_type="strategy_run",
                entity_id=run.id,
                trading_session_id=run.trading_session_id,
                strategy_config_id=config.id,
            )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
