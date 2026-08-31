"""Post-apply QC: validate the strategy_configs.params that are ACTUALLY in the
live database, not the ones we intended to write.

Feed it the output of:

    psql -A -F'|' -t -c "SELECT name, strategy_type, is_enabled,
        coalesce(runtime_mode,'NULL'), coalesce(underlying_symbol,'-'),
        params::text FROM strategy_configs WHERE ... ORDER BY name"

    python scripts/qc_paper_configs_live.py <that_file>

Runs the same checks as `qc_paper_configs.py` (API allowlist, the real exit-leg
validators, silently-dropped leg keys, actual strategy construction, lot
allocation) plus the paper/live sizing resolution, against live values.
"""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import fields
from datetime import date

sys.path.insert(0, ".")

from app.api.v1.strategies import (  # noqa: E402
    EMA_MICRO_PULLBACK_CONVICTION_PARAM_KEYS,
    LIQUIDITY_SWEEP_REVERSAL_CONVICTION_PARAM_KEYS,
    OI_VOLUME_CONFIRMED_CONVICTION_PARAM_KEYS,
    ORB_CONVICTION_PARAM_KEYS,
    VWAP_PULLBACK_CONVICTION_PARAM_KEYS,
    _build_strategy,
)
from app.domain.strategy.exit_legs import (  # noqa: E402
    ExitLegTemplate,
    allocate_leg_lots,
    deserialize_exit_leg_templates,
    validate_exit_leg_templates,
)
from app.domain.strategy.models import StrategyConfig  # noqa: E402
from app.modules.strategy_engine.sizing import (  # noqa: E402
    DEFAULT_QTY_LOTS_LIVE,
    DEFAULT_QTY_LOTS_PAPER,
)

ALLOW = {
    "orb_conviction": ORB_CONVICTION_PARAM_KEYS,
    "ema_micro_pullback_conviction": EMA_MICRO_PULLBACK_CONVICTION_PARAM_KEYS,
    "oi_volume_confirmed_conviction": OI_VOLUME_CONFIRMED_CONVICTION_PARAM_KEYS,
    "vwap_pullback_conviction": VWAP_PULLBACK_CONVICTION_PARAM_KEYS,
    "liquidity_sweep_reversal_conviction": LIQUIDITY_SWEEP_REVERSAL_CONVICTION_PARAM_KEYS,
}
LEG_FIELDS = {f.name for f in fields(ExitLegTemplate)}
ARM, LOCK = "trail_activation_fraction", "trail_lock_fraction"

# What each enabled config is expected to carry, from
# docs/ops/paper_config_update_2026_09_01.md.
EXPECT = {
    "ORB_Conviction": {"top": {"stop_pct": 0.18, "target_pct": 1.0, ARM: 0.12, LOCK: 0.6,
                               "orb_entry_cutoff_time": "10:15",
                               "max_or_range_nifty_points": 65,
                               "require_prior_day_trend": True},
                       "locks": [0.6, 0.8, 0.4, 0.6], "arms": [0.12, 0.12, 0.12, 0.12]},
    "OI_Volume_Conviction": {"top": {"stop_pct": 0.11, "target_pct": 0.18, ARM: 0.3, LOCK: 0.85,
                                     "require_atr_expansion": True,
                                     "pcr_oi_min": 0.4, "pcr_oi_max": 2.5,
                                     "oi_use_futures_volume_confirmation": False,
                                     "oi_use_atm_oi_buildup": False},
                             "locks": [0.6, 0.8, 0.8], "arms": [0.3, 0.3, 0.3]},
    "EMA_Micro_Conviction": {"top": {"stop_pct": 0.12, "target_pct": 0.12, ARM: 0.7, LOCK: 0.8,
                                     "require_prior_day_trend": True,
                                     "require_atr_expansion": True},
                             "locks": [0.6, 0.8, 0.8], "arms": [0.7, 0.7, 0.7]},
}
MUST_BE_DISABLED = {"EMA_Micro_Conviction_PCR", "VWAP_Conviction",
                    "Liquidity_Sweep_Conviction"}


def main() -> None:
    rows = [ln for ln in open(sys.argv[1], encoding="utf-8").read().splitlines() if ln.strip()]
    fail = 0
    seen = set()

    for ln in rows:
        name, stype, enabled, rmode, und, raw = ln.split("|", 5)
        params = json.loads(raw)
        seen.add(name)
        on = enabled == "t"
        print("=" * 92)
        print(f"{name}   [{stype}]   enabled={on}  runtime_mode={rmode}  underlying={und}")
        print("=" * 92)

        if name in MUST_BE_DISABLED:
            ok = not on
            print(f"  disabled as planned : {'OK' if ok else '!! STILL ENABLED'}")
            if not ok:
                fail += 1
            continue
        if not on:
            print("  !! expected ENABLED but it is disabled")
            fail += 1
            continue

        # runtime mode -- a NULL here follows the session, i.e. would route live
        if rmode != "force_paper":
            print(f"  !! runtime_mode is {rmode}, not force_paper -- would follow session mode")
            fail += 1
        else:
            print("  [0] runtime_mode    : force_paper OK")

        # 1 allowlist
        top = {k for k in params if k != "exit_legs"}
        inert = sorted(top - ALLOW[stype])
        print(f"  [1] allowlist       : {len(top)} keys, forwarded {len(top) - len(inert)}"
              + (f"  !! INERT {inert}" if inert else ""))
        if inert:
            fail += 1

        # 2 exit legs
        try:
            tpls = deserialize_exit_leg_templates(params.get("exit_legs"))
            if not tpls:
                raise ValueError("no exit_legs present")
            validate_exit_leg_templates(tpls)
            print(f"  [2] exit_legs       : OK  {len(tpls)} legs, "
                  f"qty_fraction sum = {sum(t.qty_fraction for t in tpls)}")
        except Exception as e:  # noqa: BLE001
            print(f"  [2] exit_legs       : !! {e}")
            fail += 1
            continue

        # 3 dropped keys
        dropped = set()
        for r in params["exit_legs"]:
            dropped |= set(r) - LEG_FIELDS
        msg = "OK" if not dropped else f"!! DROPPED {sorted(dropped)}"
        print(f"  [3] leg keys        : {msg}")
        if dropped:
            fail += 1

        # 4 construct + sizing
        try:
            cfg = StrategyConfig(id=uuid.uuid4(), workspace_id=uuid.uuid4(), name=name,
                                 strategy_type=stype, params=params)
            s = _build_strategy(cfg, uuid.uuid4(), date(2026, 9, 2))
            print(f"  [4] construct       : OK  -> {type(s).__name__}")
        except Exception as e:  # noqa: BLE001
            print(f"  [4] construct       : !! {type(e).__name__}: {e}")
            fail += 1
            continue

        explicit = params.get("qty_lots")
        if explicit is None:
            print(f"  [5] sizing          : OK  paper {DEFAULT_QTY_LOTS_PAPER} / "
                  f"live {DEFAULT_QTY_LOTS_LIVE} (mode-aware default)")
        else:
            print(f"  [5] sizing          : !! explicit qty_lots={explicit} in BOTH modes; "
                  "a live intent above the workspace per_trade_lot_cap is REJECTED")
            fail += 1

        # 6 lot split
        fr = [x["qty_fraction"] for x in params["exit_legs"]]
        a10 = allocate_leg_lots(DEFAULT_QTY_LOTS_PAPER, fr)
        ok10 = all(x >= 1 for x in a10)
        v = "OK" if ok10 else "!! a leg rounds to 0"
        print(f"  [6] lot split       : paper {DEFAULT_QTY_LOTS_PAPER} lots -> {a10} {v}")
        print(f"                        live {DEFAULT_QTY_LOTS_LIVE} lot -> legs collapse to "
              "the single-exit path (is_live guard), which uses the top-level params")
        if not ok10:
            fail += 1

        # 7 matches the plan
        exp = EXPECT[name]
        bad = [f"{k}={params.get(k)!r} (want {v!r})"
               for k, v in exp["top"].items() if params.get(k) != v]
        locks = [r.get(LOCK) for r in params["exit_legs"]]
        arms = [r.get(ARM) for r in params["exit_legs"]]
        if locks != exp["locks"]:
            bad.append(f"leg locks={locks} (want {exp['locks']})")
        if arms != exp["arms"]:
            bad.append(f"leg arms={arms} (want {exp['arms']})")
        print(f"  [7] matches plan    : {'OK' if not bad else '!! ' + '; '.join(bad)}")
        if bad:
            fail += 1

        # 8 the lock A/B is clean: legs 0 and 1 identical except LOCK
        a, b = dict(params["exit_legs"][0]), dict(params["exit_legs"][1])
        a.pop(LOCK, None), b.pop(LOCK, None)
        a.pop("kind", None), b.pop("kind", None)
        a.pop("qty_fraction", None), b.pop("qty_fraction", None)
        clean = a == b
        ab = "OK (legs 0,1 differ only in lock)" if clean else f"!! confounded: {(a, b)}"
        print(f"  [8] lock A/B clean  : {ab}")
        if not clean:
            fail += 1
        print()

    missing = (set(EXPECT) | MUST_BE_DISABLED) - seen
    if missing:
        print(f"!! not present in input: {sorted(missing)}")
        fail += 1

    print("=" * 92)
    print(f"LIVE QC RESULT: {'ALL CHECKS PASSED' if fail == 0 else f'{fail} PROBLEM(S)'}")
    print("=" * 92)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
