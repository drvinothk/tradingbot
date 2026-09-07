"""Careful, dry-run-first operator prep for the LIVE lot-size ramp (WS4 + WS5/B2).

Does three narrowly-scoped things, each behind an explicit flag, all in ONE
transaction, and only when `--apply` is passed (default is a dry run that
writes a rollback snapshot and changes nothing):

1. `--lot-cap N`  -> new `risk_limit_configs` version with
   `per_trade_lot_cap = N` (the server-side ceiling that *rejects*, never
   clamps, any live intent above it -- this is what actually enforces the
   1 -> 2 -> 3 lot ramp regardless of per-config `qty_lots`). No version bump
   if N already equals the active value.

2. `--pin-config NAME` (repeatable) -> `strategy_configs.runtime_mode =
   force_paper`, `runtime_mode_source = manual` for that config. Use it on a
   config that must stay on the mock broker.
   `--arm-config NAME` (repeatable) -> the inverse: `runtime_mode =
   force_live`, `runtime_mode_source = manual`. Arms the config for real
   orders in a `live_enabled` session (which, post-2026-09-08 inversion, is
   how the daily session is born). A name can't be in both lists.

3. `--rsi-block "NAME=VALUE"` (repeatable) -> sets
   `params["entry_rsi_block_pe_below"] = float(VALUE)` on that config (the
   2026-09-07 RSI-extreme entry filter, `180b41b`; inert until this key is
   set). Pass both the paper and the live duplicate config names.

Usage:
    # dry run: prints the diff, writes <rollback-out>, commits nothing
    python scripts/prep_live_ramp.py --lot-cap 1 --pin-config "Test 1" \
        --rsi-block "OI_Volume_Conviction=25" --actor-email admin@example.com

    # apply for real
    python scripts/prep_live_ramp.py --lot-cap 1 --pin-config "Test 1" \
        --rsi-block "OI_Volume_Conviction=25" --actor-email admin@example.com --apply

Safety:
- Refuses to `--apply` while any StrategyRun is non-terminal or any Position is
  OPEN, unless `--force` is also given (a lot-cap change only affects *new*
  intents and `runtime_mode`/`params` are re-read per cycle / at spawn, so it
  is not retroactive -- but the check mirrors the 2026-09-01 config-update
  discipline).
- `create_new_risk_limit_config_version` is an unlocked check-then-act (see its
  own docstring); safe here only because this is a single manual invocation.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db.session import session_scope  # noqa: E402
from app.domain.execution.models import Position, PositionStatus  # noqa: E402
from app.domain.identity.models import Role, User, UserRole, Workspace  # noqa: E402
from app.domain.strategy.models import (  # noqa: E402
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    StrategyRuntimeMode,
)
from app.modules.risk_engine.service import (  # noqa: E402
    create_new_risk_limit_config_version,
    get_active_risk_limit_config,
)

_TERMINAL_RUN_STATUSES = {StrategyRunStatus.STOPPED}


def _rm_str(value: object) -> str | None:
    """`StrategyConfig.runtime_mode` is a plain String column -- it reads back
    as `str` (or `None`), not the enum. Normalise for display/snapshot."""
    return str(value) if value is not None else None


def _resolve_workspace(db, workspace_id: str | None) -> Workspace:
    if workspace_id:
        ws = db.get(Workspace, uuid.UUID(workspace_id))
        if ws is None:
            raise SystemExit(f"no Workspace {workspace_id!r}")
        return ws
    rows = db.query(Workspace).all()
    if len(rows) != 1:
        raise SystemExit(
            f"{len(rows)} workspaces exist -- pass --workspace-id explicitly"
        )
    return rows[0]


def _resolve_actor(db, email: str | None) -> User:
    if email:
        user = db.query(User).filter(User.email == email.strip().lower()).one_or_none()
        if user is None:
            raise SystemExit(f"no User {email!r}")
        return user
    admin_role = db.query(Role).filter(Role.name == "Admin").one_or_none()
    if admin_role is None:
        raise SystemExit("no Admin role -- pass --actor-email explicitly")
    user = (
        db.query(User)
        .join(UserRole, UserRole.user_id == User.id)
        .filter(UserRole.role_id == admin_role.id)
        .order_by(User.created_at)
        .first()
    )
    if user is None:
        raise SystemExit("no Admin user -- pass --actor-email explicitly")
    return user


def _parse_rsi_specs(raw: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"--rsi-block must be NAME=VALUE, got {item!r}")
        name, _, value = item.partition("=")
        try:
            out[name.strip()] = float(value)
        except ValueError as exc:
            raise SystemExit(f"--rsi-block {item!r}: {exc}") from exc
    return out


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--lot-cap", type=int, default=None, help="new per_trade_lot_cap")
    p.add_argument(
        "--pin-config", action="append", default=[], metavar="NAME",
        help="set runtime_mode=force_paper on this config (repeatable)",
    )
    p.add_argument(
        "--arm-config", action="append", default=[], metavar="NAME",
        help="set runtime_mode=force_live on this config -- arms it for real "
             "orders in a live_enabled session (repeatable)",
    )
    p.add_argument(
        "--rsi-block", action="append", default=[], metavar="NAME=VALUE",
        help="set params.entry_rsi_block_pe_below on this config (repeatable)",
    )
    p.add_argument(
        "--actor-email", default=None, help="User to attribute the risk-config version to"
    )
    p.add_argument("--workspace-id", default=None)
    p.add_argument(
        "--rollback-out", default=None,
        help="pre-change snapshot path (default: ./prep_live_ramp_rollback_<ts>.json)",
    )
    p.add_argument("--apply", action="store_true", help="actually commit (default: dry run)")
    p.add_argument(
        "--force", action="store_true", help="apply even with open positions / running runs"
    )
    args = p.parse_args()

    if (
        args.lot_cap is None
        and not args.pin_config
        and not args.arm_config
        and not args.rsi_block
    ):
        raise SystemExit(
            "nothing to do -- pass --lot-cap and/or --pin-config and/or --arm-config "
            "and/or --rsi-block"
        )
    if args.lot_cap is not None and args.lot_cap < 1:
        raise SystemExit("--lot-cap must be >= 1")
    both = set(args.pin_config) & set(args.arm_config)
    if both:
        raise SystemExit(f"--pin-config and --arm-config both name: {sorted(both)}")

    rsi_specs = _parse_rsi_specs(args.rsi_block)
    ts_now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rollback_path = Path(args.rollback_out or f"prep_live_ramp_rollback_{ts_now}.json")

    # ---- phase 1: read-only -- resolve, snapshot, plan, preconditions ----
    with session_scope() as db:
        ws = _resolve_workspace(db, args.workspace_id)
        actor = _resolve_actor(db, args.actor_email)
        active_risk = get_active_risk_limit_config(db, ws.id)

        open_positions = (
            db.query(Position)
            .filter(Position.status == PositionStatus.OPEN)
            .count()
        )
        live_runs = (
            db.query(StrategyRun)
            .filter(StrategyRun.status.notin_(_TERMINAL_RUN_STATUSES))
            .count()
        )

        want_configs = list(
            dict.fromkeys([*args.pin_config, *args.arm_config, *rsi_specs.keys()])
        )
        cfg_rows = {
            c.name: c
            for c in db.query(StrategyConfig).filter(StrategyConfig.name.in_(want_configs)).all()
        }
        missing = [n for n in want_configs if n not in cfg_rows]
        if missing:
            raise SystemExit(f"config name(s) not found: {missing}")

        snapshot = {
            "captured_at": ts_now,
            "workspace_id": str(ws.id),
            "risk_limit_config": {
                "id": str(active_risk.id),
                "version": active_risk.version,
                "per_trade_lot_cap": active_risk.per_trade_lot_cap,
            },
            "strategy_configs": {
                c.name: {
                    "id": str(c.id),
                    "runtime_mode": _rm_str(c.runtime_mode),
                    "runtime_mode_source": c.runtime_mode_source,
                    "params": c.params,
                }
                for c in cfg_rows.values()
            },
        }
        rollback_path.write_text(json.dumps(snapshot, indent=2, default=str))
        print(f"rollback snapshot -> {rollback_path.resolve()}\n")

        # planned changes
        print("PLANNED CHANGES")
        if args.lot_cap is not None:
            if args.lot_cap == active_risk.per_trade_lot_cap:
                print(
                    f"  lot cap: already {args.lot_cap} "
                    f"(v{active_risk.version}) -- no version bump"
                )
            else:
                print(f"  lot cap: {active_risk.per_trade_lot_cap} -> {args.lot_cap} "
                      f"(new risk_limit_configs v{active_risk.version + 1}, actor {actor.email})")
        for name in args.pin_config:
            c = cfg_rows[name]
            cur = _rm_str(c.runtime_mode) or "NULL"
            if cur == StrategyRuntimeMode.FORCE_PAPER.value:
                print(f"  pin {name!r}: already force_paper -- skip")
            else:
                print(f"  pin {name!r}: runtime_mode {cur} -> force_paper, source -> manual")
        for name in args.arm_config:
            c = cfg_rows[name]
            cur = _rm_str(c.runtime_mode) or "NULL"
            if cur == StrategyRuntimeMode.FORCE_LIVE.value:
                print(f"  arm {name!r}: already force_live -- skip")
            else:
                print(f"  arm {name!r}: runtime_mode {cur} -> force_live, source -> manual")
        for name, val in rsi_specs.items():
            c = cfg_rows[name]
            cur = (c.params or {}).get("entry_rsi_block_pe_below", "<unset>")
            print(f"  rsi-block {name!r}: entry_rsi_block_pe_below {cur} -> {val}")
        print()

        blockers = open_positions or live_runs
        if blockers:
            msg = f"{open_positions} OPEN position(s), {live_runs} non-terminal StrategyRun(s)"
            if args.apply and not args.force:
                raise SystemExit(
                    f"refusing to --apply: {msg}. Re-run with --force if you have "
                    f"reviewed this (lot-cap/runtime_mode/params changes are not "
                    f"retroactive, but this check mirrors the 2026-09-01 discipline)."
                )
            note = (
                "WARNING: --force" if args.apply
                else "NOTE (dry run): --apply would need --force"
            )
            print(f"{note} -- {msg}\n")

    if not args.apply:
        print("dry run -- nothing written. Re-run with --apply to commit.")
        return

    # ---- phase 2: write ----
    with session_scope() as db:
        ws = _resolve_workspace(db, args.workspace_id)
        actor = _resolve_actor(db, args.actor_email)

        if args.lot_cap is not None:
            active_risk = get_active_risk_limit_config(db, ws.id)
            if args.lot_cap != active_risk.per_trade_lot_cap:
                new_cfg = create_new_risk_limit_config_version(
                    db, ws.id, actor_user=actor,
                    reason=f"live lot ramp: per_trade_lot_cap -> {args.lot_cap}",
                    per_trade_lot_cap=args.lot_cap,
                )
                print(
                    f"  risk_limit_configs v{new_cfg.version}: "
                    f"per_trade_lot_cap = {args.lot_cap}"
                )

        for name in args.pin_config:
            c = db.query(StrategyConfig).filter(StrategyConfig.name == name).one()
            if _rm_str(c.runtime_mode) != StrategyRuntimeMode.FORCE_PAPER.value:
                c.runtime_mode = StrategyRuntimeMode.FORCE_PAPER
                c.runtime_mode_source = "manual"
                db.add(c)
                print(f"  {name!r}: runtime_mode = force_paper")

        for name in args.arm_config:
            c = db.query(StrategyConfig).filter(StrategyConfig.name == name).one()
            if _rm_str(c.runtime_mode) != StrategyRuntimeMode.FORCE_LIVE.value:
                c.runtime_mode = StrategyRuntimeMode.FORCE_LIVE
                c.runtime_mode_source = "manual"
                db.add(c)
                print(f"  {name!r}: runtime_mode = force_live")

        for name, val in rsi_specs.items():
            c = db.query(StrategyConfig).filter(StrategyConfig.name == name).one()
            c.params = {**(c.params or {}), "entry_rsi_block_pe_below": val}
            db.add(c)
            print(f"  {name!r}: params.entry_rsi_block_pe_below = {val}")

    print("\napplied. rollback: restore the values in the snapshot file above.")


if __name__ == "__main__":
    main()
