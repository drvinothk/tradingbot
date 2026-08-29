"""The shape of `Signal.exit_legs` / `TradeIntent.exit_legs` (multi-leg exit
engine) plus the pure helpers that operate on it.

Two related shapes:

- **`ExitLegTemplate`** — the *static per-strategy* config, stored in
  `strategy_config.params["exit_legs"]` as a list of dicts. Percentages and
  flags only, no absolute prices (those depend on the runtime signal).
  Validated by `validate_exit_leg_templates` at config create/update.

- **`ExitLegSpec`** — the *per-signal* concrete leg, carried on
  `TradeProposal.exit_legs` and serialised onto `Signal`/`TradeIntent`.
  Absolute prices/levels, produced from a template list + the signal's own
  entry/stop/target by `build_exit_legs`.

`execution_engine.paper.service._open_position_from_fill` deserialises
`TradeIntent.exit_legs` (spec form) and builds one `PositionExitLeg` per spec,
allocating whole lots via `allocate_leg_lots`.

`None` (no spec) everywhere means "single full-qty exit" — today's behaviour,
byte-identical, with no leg rows created at all.

Lives in the domain layer (not `strategy_engine`) because it defines the
persisted shape of two domain columns and is read by both the strategy and the
execution bounded contexts; it imports nothing from either.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

# A staged exit must have between 2 and this many legs. A "1-leg staged exit"
# is just a normal single exit, so it is rejected at config time (use the
# strategy's plain stop_pct/target_pct instead). The ceiling keeps a
# fat-fingered config from spawning dozens of resting broker orders.
MIN_EXIT_LEGS = 2
MAX_EXIT_LEGS = 6
# qty_fraction values must sum to 1.0 within this tolerance (float slack).
FRACTION_SUM_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ExitLegTemplate:
    """Static per-strategy leg config (`strategy_config.params["exit_legs"]`).

    `stop_pct`/`target_pct` are fractions off entry (0.18 == 18%), matching
    `strategy_engine.common_rules.compute_stop_target`. `None` for either means
    "use the signal's base stop/target". `no_target=True` marks a runner leg
    (overrides `target_pct`). `use_structure=True` copies the signal's
    structure-break level/buffer/persistence onto this leg.
    """

    qty_fraction: float
    kind: str = "custom"
    stop_pct: float | None = None
    target_pct: float | None = None
    no_target: bool = False
    use_structure: bool = False
    trail_activation_fraction: float | None = None
    trail_lock_fraction: float | None = None
    max_loss_per_lot: float | None = None
    time_stop_minutes: float | None = None


@dataclass(frozen=True)
class ExitLegSpec:
    """One concrete leg of a staged exit (per-signal).

    `qty_fraction` (0 < f <= 1; all legs sum to 1.0) is resolved to whole lots
    at dispatch by `allocate_leg_lots`. Every price/level field is an absolute
    value on the same basis as `TradeProposal`'s own (`stop_price`/
    `target_price` on the option premium, `structure_level` on the underlying
    index). `target_price=None` marks a runner leg (exits only on its
    stop/structure/trail/EOD/margin backstops).
    """

    qty_fraction: float
    kind: str = "custom"
    stop_price: float | None = None
    target_price: float | None = None
    structure_level: float | None = None
    structure_break_buffer: float | None = None
    structure_break_persistence_seconds: float | None = None
    trail_activation_fraction: float | None = None
    trail_lock_fraction: float | None = None
    max_loss_per_lot: float | None = None
    time_stop_minutes: float | None = None


def _filtered(cls: type, raw: dict) -> dict:
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in raw.items() if k in known}


def deserialize_exit_leg_templates(raw: object) -> list[ExitLegTemplate] | None:
    """Parse `strategy_config.params["exit_legs"]`. Tolerant of unknown keys."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("params.exit_legs must be a list of leg objects")
    out: list[ExitLegTemplate] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"each exit leg must be an object, got {type(item).__name__}")
        try:
            out.append(ExitLegTemplate(**_filtered(ExitLegTemplate, item)))
        except TypeError as exc:  # missing qty_fraction, wrong type, ...
            raise ValueError(f"invalid exit leg object {item!r}: {exc}") from exc
    return out or None


def serialize_exit_legs(specs: list[ExitLegSpec] | None) -> list[dict] | None:
    if not specs:
        return None
    return [asdict(s) for s in specs]


def deserialize_exit_legs(raw: object) -> list[ExitLegSpec] | None:
    """Parse `TradeIntent.exit_legs` (spec form). Tolerant of unknown keys."""
    if not raw or not isinstance(raw, list):
        return None
    out: list[ExitLegSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"exit leg spec must be an object, got {type(item).__name__}")
        out.append(ExitLegSpec(**_filtered(ExitLegSpec, item)))
    return out or None


def validate_exit_leg_templates(templates: list[ExitLegTemplate]) -> None:
    """Raises `ValueError` on any structural problem. Called by the strategy
    -config create/update endpoints (reject 422).
    """
    if len(templates) < MIN_EXIT_LEGS:
        raise ValueError(
            f"exit_legs must contain at least {MIN_EXIT_LEGS} legs "
            f"(a 1-leg staged exit is just a normal exit), got {len(templates)}"
        )
    if len(templates) > MAX_EXIT_LEGS:
        raise ValueError(
            f"exit_legs supports at most {MAX_EXIT_LEGS} legs, got {len(templates)}"
        )
    for i, leg in enumerate(templates):
        if not (0 < leg.qty_fraction <= 1):
            raise ValueError(
                f"leg {i}: qty_fraction must be in (0, 1], got {leg.qty_fraction}"
            )
        for name, val in (("stop_pct", leg.stop_pct), ("target_pct", leg.target_pct)):
            if val is not None and not (0 < val < 1):
                raise ValueError(f"leg {i}: {name} must be in (0, 1), got {val}")
        if leg.no_target and leg.target_pct is not None:
            raise ValueError(f"leg {i}: cannot set both no_target and target_pct")
    total = sum(leg.qty_fraction for leg in templates)
    if abs(total - 1.0) > FRACTION_SUM_TOLERANCE:
        raise ValueError(
            f"exit_legs qty_fraction values must sum to 1.0, got {total}"
        )


def validate_exit_leg_specs(specs: list[ExitLegSpec], *, is_buy: bool) -> None:
    """Defensive check at dispatch — `specs` come from `build_exit_legs`, so
    this mostly guards against a hand-crafted `TradeIntent.exit_legs`.
    """
    if not specs:
        raise ValueError("exit_legs must contain at least one leg")
    if len(specs) > MAX_EXIT_LEGS:
        raise ValueError(f"exit_legs supports at most {MAX_EXIT_LEGS} legs, got {len(specs)}")
    for i, leg in enumerate(specs):
        if not (0 < leg.qty_fraction <= 1):
            raise ValueError(f"leg {i}: qty_fraction must be in (0, 1], got {leg.qty_fraction}")
        if leg.stop_price is not None and leg.target_price is not None:
            if is_buy and not leg.stop_price < leg.target_price:
                raise ValueError(
                    f"leg {i}: stop_price {leg.stop_price} must be below "
                    f"target_price {leg.target_price} for a long position"
                )
            if not is_buy and not leg.stop_price > leg.target_price:
                raise ValueError(
                    f"leg {i}: stop_price {leg.stop_price} must be above "
                    f"target_price {leg.target_price} for a short position"
                )
    total = sum(leg.qty_fraction for leg in specs)
    if abs(total - 1.0) > FRACTION_SUM_TOLERANCE:
        raise ValueError(f"exit_legs qty_fraction values must sum to 1.0, got {total}")


def build_exit_legs(
    templates: list[ExitLegTemplate],
    *,
    entry_price: float,
    base_stop_price: float,
    base_target_price: float,
    tick_size: float,
    structure_level: float | None = None,
    structure_break_buffer: float | None = None,
    structure_break_persistence_seconds: float | None = None,
) -> list[ExitLegSpec]:
    """Resolve static templates into concrete per-signal specs. A per-leg
    `stop_pct`/`target_pct` overrides the base price for that leg; `None` uses
    the base. `no_target` legs get `target_price=None`. `use_structure` legs
    inherit the signal's structure-break trio. `_round_to_tick` keeps the
    per-leg overrides tradable (imported lazily to avoid a domain->module
    import edge).
    """
    from app.modules.strategy_engine.common_rules import _round_to_tick

    specs: list[ExitLegSpec] = []
    for leg in templates:
        stop_price = (
            _round_to_tick(entry_price * (1 - leg.stop_pct), tick_size)
            if leg.stop_pct is not None
            else base_stop_price
        )
        if leg.no_target:
            target_price: float | None = None
        elif leg.target_pct is not None:
            target_price = _round_to_tick(entry_price * (1 + leg.target_pct), tick_size)
        else:
            target_price = base_target_price
        specs.append(
            ExitLegSpec(
                qty_fraction=leg.qty_fraction,
                kind=leg.kind,
                stop_price=stop_price,
                target_price=target_price,
                structure_level=structure_level if leg.use_structure else None,
                structure_break_buffer=(
                    structure_break_buffer if leg.use_structure else None
                ),
                structure_break_persistence_seconds=(
                    structure_break_persistence_seconds if leg.use_structure else None
                ),
                trail_activation_fraction=leg.trail_activation_fraction,
                trail_lock_fraction=leg.trail_lock_fraction,
                max_loss_per_lot=leg.max_loss_per_lot,
                time_stop_minutes=leg.time_stop_minutes,
            )
        )
    return specs


def allocate_leg_lots(total_lots: int, fractions: list[float]) -> list[int]:
    """Split `total_lots` across `fractions` using largest-remainder rounding,
    with any leftover lot(s) assigned to the leg(s) with the largest fractional
    part, ties broken toward *later* legs (so the runner is favoured).
    Deterministic. Does not itself guarantee every leg >= 1 — the collapse
    guard in `_open_position_from_fill` handles the "a leg rounds to 0" case.
    """
    if total_lots <= 0 or not fractions:
        return [0 for _ in fractions]
    raw = [total_lots * f for f in fractions]
    floors = [int(x) for x in raw]
    remainder = total_lots - sum(floors)
    order = sorted(
        range(len(fractions)),
        key=lambda i: (raw[i] - floors[i], i),
        reverse=True,
    )
    for idx in order[: max(0, remainder)]:
        floors[idx] += 1
    return floors
