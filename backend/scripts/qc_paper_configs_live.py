"""Post-apply structural QC for `strategy_configs.params` as they ACTUALLY sit in
the database -- run it after any config apply, against the live rows, not the
JSON you meant to write.

    psql -A -F'|' -t -c "SELECT name, strategy_type, is_enabled, runtime_mode,
        coalesce(underlying_symbol,'-'), coalesce(updated_at::text,'-'),
        params::text
      FROM strategy_configs
      WHERE is_enabled ORDER BY name" > rows.txt

    python scripts/qc_paper_configs_live.py rows.txt

Per row, against this repo's own live imports (not a reimplementation):

  [0] runtime_mode -- reported + summarised; fails the run only on a value
      that is not a valid StrategyRuntimeMode. Since the 2026-09-08 paper/live
      inversion (migration 0039) the column is non-nullable and exactly one of
      force_paper | force_live -- there is no "follow the session" (NULL)
      state. force_live places real orders whenever the session is
      LIVE_ENABLED, and daily sessions are now *born* LIVE_ENABLED.
  [1] every top-level param key is in that strategy_type's `_build_strategy`
      allowlist -- anything else is stored but silently does nothing
  [2] params.exit_legs passes deserialize_exit_leg_templates + the real
      validate_exit_leg_templates (leg count, qty_fraction sum, pct ranges,
      no_target/target_pct exclusivity); a config with no exit_legs is fine
  [3] no per-leg key is silently dropped by ExitLegTemplate
  [4] _build_strategy actually constructs the strategy with the forwarded params
  [5] sizing resolution (informational -- 2026-09-04 model)
  [6] lot split at real sizes via allocate_leg_lots_floored (informational)

Deliberately NOT here: conformance to any one apply's *intended* values. That is
snapshot-specific and goes stale the moment the config set moves on (it is why
the pre-2026-09-08 version of this script KeyError'd on a renamed / base-type
row). For "did my apply land exactly as intended", diff the rows around it:

    diff <(psql ... 'SELECT ... ORDER BY name')   # before the apply
         <(psql ... 'SELECT ... ORDER BY name')   # after
"""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import fields
from datetime import date

sys.path.insert(0, ".")

from app.api.v1.strategies import (  # noqa: E402
    ATR_BREAKOUT_PARAM_KEYS,
    EMA_MICRO_PULLBACK_CONVICTION_PARAM_KEYS,
    EMA_MICRO_PULLBACK_PARAM_KEYS,
    KNOWN_STRATEGY_TYPES,
    LIQUIDITY_SWEEP_REVERSAL_CONVICTION_PARAM_KEYS,
    LIQUIDITY_SWEEP_REVERSAL_PARAM_KEYS,
    OI_VOLUME_CONFIRMED_CONVICTION_PARAM_KEYS,
    OI_VOLUME_CONFIRMED_PARAM_KEYS,
    ORB_CONVICTION_PARAM_KEYS,
    ORB_PARAM_KEYS,
    VWAP_PULLBACK_CONVICTION_PARAM_KEYS,
    VWAP_PULLBACK_PARAM_KEYS,
    _build_strategy,
)
from app.domain.strategy.exit_legs import (  # noqa: E402
    ExitLegTemplate,
    allocate_leg_lots_floored,
    deserialize_exit_leg_templates,
    validate_exit_leg_templates,
)
from app.domain.strategy.models import StrategyConfig  # noqa: E402
from app.modules.strategy_engine.sizing import (  # noqa: E402
    DEFAULT_QTY_LOTS_LIVE,
    DEFAULT_QTY_LOTS_PAPER,
)

# strategy_type -> its exact `_build_strategy` allowlist. Every type the app
# knows; `synthetic` takes no tunables. Asserted complete against
# KNOWN_STRATEGY_TYPES so a new strategy type added upstream fails here loudly
# instead of KeyError'ing mid-run against a live row.
ALLOW: dict[str, set[str]] = {
    "synthetic": set(),
    "orb": ORB_PARAM_KEYS,
    "orb_conviction": ORB_CONVICTION_PARAM_KEYS,
    "atr_breakout": ATR_BREAKOUT_PARAM_KEYS,
    "vwap_pullback": VWAP_PULLBACK_PARAM_KEYS,
    "vwap_pullback_conviction": VWAP_PULLBACK_CONVICTION_PARAM_KEYS,
    "ema_micro_pullback": EMA_MICRO_PULLBACK_PARAM_KEYS,
    "ema_micro_pullback_conviction": EMA_MICRO_PULLBACK_CONVICTION_PARAM_KEYS,
    "oi_volume_confirmed": OI_VOLUME_CONFIRMED_PARAM_KEYS,
    "oi_volume_confirmed_conviction": OI_VOLUME_CONFIRMED_CONVICTION_PARAM_KEYS,
    "liquidity_sweep_reversal": LIQUIDITY_SWEEP_REVERSAL_PARAM_KEYS,
    "liquidity_sweep_reversal_conviction": LIQUIDITY_SWEEP_REVERSAL_CONVICTION_PARAM_KEYS,
}
assert set(ALLOW) == KNOWN_STRATEGY_TYPES, (
    "ALLOW out of sync with KNOWN_STRATEGY_TYPES: "
    f"missing {KNOWN_STRATEGY_TYPES - set(ALLOW)}, extra {set(ALLOW) - KNOWN_STRATEGY_TYPES}"
)

LEG_FIELDS = {f.name for f in fields(ExitLegTemplate)}


def _split_row(ln: str) -> tuple[str, str, bool, str, str, dict]:
    """`name|stype|enabled|runtime_mode|underlying|updated_at|params_json`.

    `updated_at` is optional -- older SELECT templates emit 6 fields, the
    current one 7. `params` is always last so a `|` inside the JSON is safe.
    """
    parts = ln.split("|", 6)
    if len(parts) == 7:
        name, stype, enabled, rmode, und, _updated, raw = parts
    elif len(parts) == 6:
        name, stype, enabled, rmode, und, raw = parts
    else:
        raise ValueError(f"cannot parse row (need 6 or 7 |-fields): {ln!r}")
    return name, stype, enabled == "t", rmode, und, json.loads(raw)


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    rows = [ln for ln in open(sys.argv[1], encoding="utf-8").read().splitlines() if ln.strip()]
    fail = 0
    checked = 0
    routes_live: list[str] = []

    for ln in rows:
        try:
            name, stype, on, rmode, und, params = _split_row(ln)
        except ValueError as e:
            print(f"!! {e}")
            fail += 1
            continue
        checked += 1
        print("=" * 92)
        print(f"{name}   [{stype}]   enabled={on}  runtime_mode={rmode}  underlying={und}")
        print("=" * 92)

        # [0] runtime_mode -- since migration 0039 the column is non-nullable
        # and exactly force_paper | force_live (no "follow the session" state).
        if rmode == "force_paper":
            print("  [0] runtime_mode    : force_paper (never real money)")
        elif rmode == "force_live":
            print("  [0] runtime_mode    : force_live -> real orders whenever the session "
                  "is LIVE_ENABLED (sessions are now born LIVE_ENABLED)")
            routes_live.append(name)
        else:
            print(f"  [0] runtime_mode    : !! {rmode!r} is not a valid StrategyRuntimeMode "
                  "(expected force_paper | force_live; column non-nullable since 0039)")
            fail += 1

        legs_raw = params.get("exit_legs")

        # [1] allowlist -- every top-level key must reach the constructor.
        allow = ALLOW.get(stype)
        if allow is None:
            print(f"  [1] allowlist       : !! unknown strategy_type '{stype}' "
                  "(not in KNOWN_STRATEGY_TYPES) -- skipping [1] and [4]")
            fail += 1
        else:
            top = {k for k in params if k != "exit_legs"}
            inert = sorted(top - allow)
            print(f"  [1] allowlist       : {len(top)} keys, forwarded {len(top) - len(inert)}"
                  + (f"  !! INERT (stored, does nothing) {inert}" if inert else ""))
            if inert:
                fail += 1

        # [2] exit_legs -- a plain single-exit config (no exit_legs) is valid.
        tpls = None
        if legs_raw is None:
            print("  [2] exit_legs       : (none -- single-exit config)")
        else:
            try:
                tpls = deserialize_exit_leg_templates(legs_raw)
                if not tpls:
                    raise ValueError("exit_legs present but empty")
                validate_exit_leg_templates(tpls)
                print(f"  [2] exit_legs       : OK  {len(tpls)} legs, "
                      f"qty_fraction sum = {sum(t.qty_fraction for t in tpls)}")
            except Exception as e:  # noqa: BLE001
                print(f"  [2] exit_legs       : !! {e}")
                fail += 1
                tpls = None  # invalid -> don't show a [6] lot split for it

        # [3] no per-leg key silently dropped by ExitLegTemplate
        if isinstance(legs_raw, list):
            dropped = set()
            for r in legs_raw:
                if isinstance(r, dict):
                    dropped |= set(r) - LEG_FIELDS
            verdict = "OK" if not dropped else f"!! DROPPED {sorted(dropped)}"
            print(f"  [3] leg keys        : {verdict}")
            if dropped:
                fail += 1

        # [4] actually construct the strategy with the forwarded params
        if allow is not None:
            try:
                cfg = StrategyConfig(id=uuid.uuid4(), workspace_id=uuid.uuid4(),
                                     name=name, strategy_type=stype, params=params)
                s = _build_strategy(cfg, uuid.uuid4(), date(2026, 9, 2))
                print(f"  [4] construct       : OK  -> {type(s).__name__}")
            except Exception as e:  # noqa: BLE001
                print(f"  [4] construct       : !! {type(e).__name__}: {e}")
                fail += 1

        # [5] sizing (informational -- 2026-09-04 model: an explicit qty_lots
        # wins only when routed live; paper always uses the paper default).
        explicit = params.get("qty_lots")
        if explicit is None:
            print(f"  [5] sizing          : paper {DEFAULT_QTY_LOTS_PAPER} / live "
                  f"{DEFAULT_QTY_LOTS_LIVE} (mode-aware default, no override)")
        else:
            print(f"  [5] sizing          : explicit qty_lots={explicit} -- honored only when "
                  f"routed live; paper still uses {DEFAULT_QTY_LOTS_PAPER}. Risk Service "
                  "rejects (never clamps) a live intent above per_trade_lot_cap.")

        # [6] lot split at real sizes (informational). build_position_exit_legs
        # uses allocate_leg_lots_floored: keeps the min(legs, lots)
        # largest-fraction legs (>=1 each), drops the rest with an
        # exit_legs_reduced alert. 1 lot / non-lot-multiple fill collapses to a
        # single exit sourced from the dominant (highest qty_fraction) leg via
        # pick_collapsed_exit_leg -- NOT the top-level params. LIVE builds legs
        # the same way as paper (since 2026-09-04).
        if tpls:
            fr = [t.qty_fraction for t in tpls]
            for n in (DEFAULT_QTY_LOTS_PAPER, 3, 2):
                kept, dropped_idx = allocate_leg_lots_floored(n, fr)
                note = f"  drops leg(s) {dropped_idx}" if dropped_idx else ""
                print(f"  [6] lot split n={n:<2} : {kept}{note}")
            print("                        n=1 -> single exit from the dominant leg "
                  f"(max qty_fraction = {max(fr)})")
        print()

    print("=" * 92)
    if routes_live:
        print(f"ROUTES LIVE ({len(routes_live)}): " + ", ".join(routes_live))
    print(f"{checked} config(s) checked -- "
          + ("ALL STRUCTURAL CHECKS PASSED" if fail == 0 else f"{fail} PROBLEM(S)"))
    print("=" * 92)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
