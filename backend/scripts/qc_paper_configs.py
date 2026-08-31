"""QC the exact strategy_config.params we intend to write for paper trading.

Checks, per config:
  1. every top-level key is in that strategy_type's API allowlist
     (a non-allowlisted key is stored but NEVER forwarded -> silently inert)
  2. exit_legs parse + validate through the real domain validators
  3. every exit-leg key survives ExitLegTemplate's _filtered() drop
  4. the strategy object actually constructs with the forwarded params
     (catches a TypeError that would only show up at start_strategy time)
  5. diff vs what is deployed on OCI right now
"""
from __future__ import annotations

import json
import sys
from dataclasses import fields

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
    deserialize_exit_leg_templates,
    validate_exit_leg_templates,
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


def leg(kind, frac, stop, *, target=None, no_target=False, arm=0.12, lock=0.6):
    d = {"kind": kind, "qty_fraction": frac, "stop_pct": stop,
         "use_structure": True, ARM: arm, LOCK: lock}
    if no_target:
        d["no_target"] = True
    if target is not None:
        d["target_pct"] = target
    return d


# ---------------------------------------------------------------- proposed
# Leg design (all three): legs 0 and 1 are IDENTICAL except trail_lock_fraction
# (0.6 vs 0.8) -> a clean live A/B on the one exit question the 1-min backtest
# structurally cannot answer (sub-minute adverse wicks). Leg 2 is the
# "as required" leg and varies ONE other thing, so it answers a second
# question without confounding the lock A/B.
# qty_fraction 0.4/0.3/0.3 -> allocate_leg_lots(10, ...) == [4, 3, 3].
# Top-level stop/target/arm/lock are NOT redundant: build_position_exit_legs
# returns None for a LIVE position (and for <3 lots), collapsing to the
# single-exit path, which reads exactly these top-level values.
FINAL = {
    "ORB_Conviction": ("orb_conviction", {
        "require_prior_day_trend": True,
        "max_or_range_nifty_points": 65,
        "orb_entry_cutoff_time": "10:15",
        "stop_pct": 0.18, "target_pct": 1.0, ARM: 0.12, LOCK: 0.6,
        "exit_legs": [
            # anchor leg. Legs 1-3 each differ from THIS leg in exactly one
            # field, so all three comparisons are single-variable.
            leg("core",      0.3, 0.18, no_target=True, arm=0.12, lock=0.6),
            leg("runner",    0.3, 0.18, no_target=True, arm=0.12, lock=0.8),
            leg("tightlock", 0.2, 0.18, no_target=True, arm=0.12, lock=0.4),
            leg("target",    0.2, 0.18, target=0.40,    arm=0.12, lock=0.6),
        ],
    }),
    "OI_Volume_Conviction": ("oi_volume_confirmed_conviction", {
        "oi_use_futures_volume_confirmation": False,
        "oi_use_atm_oi_buildup": False,
        "require_atr_expansion": True,
        "pcr_oi_min": 0.4, "pcr_oi_max": 2.5,
        "stop_pct": 0.11, "target_pct": 0.18, ARM: 0.30, LOCK: 0.85,
        "exit_legs": [
            leg("core",   0.4, 0.11, arm=0.30, lock=0.6),
            leg("runner", 0.3, 0.11, arm=0.30, lock=0.8),
            leg("wide",   0.3, 0.17, arm=0.30, lock=0.8),
        ],
    }),
    "EMA_Micro_Conviction": ("ema_micro_pullback_conviction", {
        "require_prior_day_trend": True,
        "require_atr_expansion": True,
        "stop_pct": 0.12, "target_pct": 0.12, ARM: 0.70, LOCK: 0.8,
        "exit_legs": [
            leg("core",   0.4, 0.12, arm=0.70, lock=0.6),
            leg("runner", 0.3, 0.12, arm=0.70, lock=0.8),
            leg("tight",  0.3, 0.06, arm=0.70, lock=0.8),
        ],
    }),
}

# ---------------------------------------------------------------- deployed
DEPLOYED = {
    "ORB_Conviction": {"stop_pct": 0.18, "target_pct": 1.0, LOCK: 0.6,
                       "orb_entry_cutoff_time": "10:00", "require_prior_day_trend": True,
                       "max_or_range_nifty_points": 65, ARM: 0.12},
    "OI_Volume_Conviction": {"qty_lots": 10, "pcr_oi_max": 2.5, "pcr_oi_min": 0.4,
                             "oi_use_atm_oi_buildup": False, "require_atr_expansion": True,
                             "oi_use_futures_volume_confirmation": False,
                             "exit_legs": [
                                 leg("core", 0.4, 0.11, arm=0.5, lock=0.8),
                                 leg("wide", 0.3, 0.17, arm=0.5, lock=0.8),
                                 leg("tight", 0.3, 0.09, arm=0.5, lock=0.8)]},
    "EMA_Micro_Conviction": {"qty_lots": 10, "require_atr_expansion": True,
                             "require_prior_day_trend": True,
                             "exit_legs": [
                                 leg("core", 0.4, 0.12, arm=0.5, lock=0.8),
                                 leg("tight", 0.3, 0.06, arm=0.5, lock=0.8),
                                 leg("mid", 0.3, 0.08, arm=0.5, lock=0.8)]},
}

fail = 0
for name, (stype, params) in FINAL.items():
    print("=" * 92)
    print(f"{name}   [{stype}]")
    print("=" * 92)
    allow = ALLOW[stype]

    # 1 -- allowlist
    top = {k for k in params if k != "exit_legs"}
    inert = sorted(top - allow)
    print(f"  [1] allowlist       : {len(top)} keys, forwarded {len(top) - len(inert)}")
    if inert:
        fail += 1
        print(f"      !! SILENTLY INERT (stored, never forwarded): {inert}")

    # 2 -- exit_legs through the real validators
    try:
        tpls = deserialize_exit_leg_templates(params["exit_legs"])
        validate_exit_leg_templates(tpls)
        tot = sum(t.qty_fraction for t in tpls)
        print(f"  [2] exit_legs       : OK  {len(tpls)} legs, qty_fraction sum = {tot}")
    except Exception as e:  # noqa: BLE001
        fail += 1
        print(f"  [2] exit_legs       : !! REJECTED -> {e}")
        continue

    # 3 -- silently dropped leg keys
    dropped = set()
    for raw in params["exit_legs"]:
        dropped |= set(raw) - LEG_FIELDS
    msg = "OK" if not dropped else "!! DROPPED " + str(sorted(dropped))
    print(f"  [3] leg keys        : {msg}")
    if dropped:
        fail += 1

    # 4 -- does the strategy actually construct?
    try:
        import uuid as _uuid
        from datetime import date as _date

        from app.domain.strategy.models import StrategyConfig as _SC
        cfg = _SC(id=_uuid.uuid4(), workspace_id=_uuid.uuid4(), name=name,
                  strategy_type=stype, params=params)
        s = _build_strategy(cfg, _uuid.uuid4(), _date(2026, 9, 2))
        print(f"  [4] construct       : OK  -> {type(s).__name__}")
        for attr, want in ((ARM, params.get(ARM)), (LOCK, params.get(LOCK)),
                           ("orb_entry_cutoff_time", params.get("orb_entry_cutoff_time"))):
            if want is not None and hasattr(s, attr):
                print(f"      {attr} = {getattr(s, attr)!r}  (asked {want!r})")
        from app.modules.strategy_engine.sizing import DEFAULT_QTY_LOTS_LIVE, DEFAULT_QTY_LOTS_PAPER
        exp = params.get("qty_lots")
        q = (f"explicit {exp} in BOTH modes  !! bypasses the live floor"
             if exp is not None else
             f"paper {DEFAULT_QTY_LOTS_PAPER} / live {DEFAULT_QTY_LOTS_LIVE} (mode-aware default)")
        print(f"      qty_lots -> {q}")
        if exp is not None:
            fail += 1
    except Exception as e:  # noqa: BLE001
        fail += 1
        print(f"  [4] construct       : !! {type(e).__name__}: {e}")

    # 6 -- lot allocation at the sizes this will actually run at
    from app.domain.strategy.exit_legs import allocate_leg_lots
    fr = [x["qty_fraction"] for x in params["exit_legs"]]
    a10, a1 = allocate_leg_lots(10, fr), allocate_leg_lots(1, fr)
    ok10 = all(x >= 1 for x in a10)
    v = "OK" if ok10 else "!! a leg rounds to 0"
    print(f"  [6] lot split       : paper 10 lots -> {a10} {v}")
    print(f"                        live   1 lot  -> {a1} -> legs COLLAPSE to the "
          "single-exit path (is_live guard fires first), which uses top-level params")
    if not ok10:
        fail += 1

    # 5 -- diff vs deployed
    dep = DEPLOYED.get(name, {})
    print("  [5] diff vs deployed:")
    for k in sorted(set(dep) | set(params)):
        if k == "exit_legs":
            continue
        a, b = dep.get(k, "<absent>"), params.get(k, "<absent>")
        if a != b:
            print(f"        {k:32s} {a!r}  ->  {b!r}")
    da, db = dep.get("exit_legs", []), params.get("exit_legs", [])
    for i in range(max(len(da), len(db))):
        x = da[i] if i < len(da) else {}
        y = db[i] if i < len(db) else {}
        d = {k: (x.get(k, "<absent>"), y.get(k, "<absent>"))
             for k in sorted(set(x) | set(y)) if x.get(k) != y.get(k)}
        if d:
            print(f"        leg[{i}] {y.get('kind', x.get('kind'))}: " +
                  ", ".join(f"{k} {v[0]!r}->{v[1]!r}" for k, v in d.items()))
    print()

print("=" * 92)
print(f"QC RESULT: {'ALL CHECKS PASSED' if fail == 0 else f'{fail} PROBLEM(S)'}")
print("=" * 92)
print("\nSQL-ready JSON:\n")
for name, (_st, p) in FINAL.items():
    print(f"-- {name}")
    print(json.dumps(p, separators=(",", ":")))
    print()
