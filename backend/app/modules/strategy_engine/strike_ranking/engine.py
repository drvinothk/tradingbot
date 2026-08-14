"""Shared strike-ranking engine. `rank_strikes` is pure — no DB/broker
dependency, fully testable against synthetic contracts — mirroring the same
split `market_data.indicators.engine.IndicatorEngine` uses: pure scoring
logic here, `rank_from_latest_snapshot` below is the thin DB-loading wrapper
every strategy (synthetic now, ORB/VWAP/EMA from Phase 4) calls instead.

ATM±N is by **strike index**, not price distance — Nifty's 50-point and Bank
Nifty's 100-point strike steps mean "ATM ± 200" would include a different
number of strikes for each; ranking by index keeps `atm_range=3` meaning the
same thing ("3 strikes either side of ATM") for every underlying.

A wide bid-ask spread is a hard filter, not just a scoring penalty — no
combination of good volume/OI can compensate for a contract where slippage
eats the trade before it starts.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, time

from sqlalchemy.orm import Session

from app.domain.market.models import OptionType


@dataclass(frozen=True)
class StrikeRankingConfig:
    atm_range: int = 3
    max_spread_pct: float = 0.15
    preferred_premium_min: float = 20.0
    preferred_premium_max: float = 150.0
    weight_spread: float = 0.30
    weight_volume: float = 0.20
    weight_oi: float = 0.20
    weight_premium_fit: float = 0.20
    weight_depth: float = 0.10
    # Hard participation floor, alongside max_spread_pct — a candidate below
    # either gets excluded before scoring, not merely penalized. Both
    # default to 0 (no filtering), preserving every existing caller's
    # behavior unchanged; the OI/Volume Confirmed strategy (Phase 7) is the
    # first to set these to non-zero values.
    min_oi: int = 0
    min_volume: int = 0

    # Ops-Hardening Phase 1 (2026-08-14): DTE/time-of-day strike-window
    # overrides — inert unless rank_strikes()/rank_from_latest_snapshot() is
    # called with *both* dte and current_time (see those functions' own
    # docstrings for why "both or neither", never one alone). Every default
    # below reproduces the plain ATM±atm_range window when DTE-awareness
    # isn't requested, so every existing caller and test is byte-for-byte
    # unaffected by these fields existing.
    non_expiry_day_window: int = 1  # "ATM to 1 strike ITM/OTM" -- symmetric
    expiry_morning_window: int = 1  # "ATM to 1-ITM" -- ITM side only, per option_type
    # Hard filter (excludes, not just penalizes), same shape as max_spread_pct
    # above — only applied during the expiry-morning window, per the
    # original spec ("morning expiry trades ... with a premium floor").
    # 0.0 = disabled. Deliberately not hardcoded to any specific rupee value
    # here — that's a real strategy's own StrikeRankingConfig to set.
    expiry_morning_premium_floor: float = 0.0
    # Strike-index distance from ATM, in the ITM direction, that expiry
    # afternoon anchors its window on -- "deep ITM to completely avoid theta
    # decay traps" means several strikes away, not adjacent to ATM (that's
    # what the morning window already covers).
    expiry_afternoon_deep_itm_offset: int = 3
    expiry_afternoon_window: int = 1  # band width around that deep-ITM anchor
    # The morning/afternoon boundary on expiry day itself (IST). Noon is a
    # reasonable, easily-adjusted default -- exposed here rather than
    # hardcoded inline so a real strategy can tune it without touching this
    # module.
    expiry_day_afternoon_cutoff: time = time(12, 0)


@dataclass(frozen=True)
class RankableContract:
    contract_symbol: str
    option_contract_id: uuid.UUID
    strike: float
    option_type: OptionType
    ltp: float
    bid: float
    ask: float
    volume: int
    oi: int
    # Best-effort top-of-book liquidity (sum of bid+ask qty across levels).
    # None when no depth data is available yet — scored neutrally rather
    # than penalized, since depth is the one input the mock chain doesn't
    # always have alongside it.
    depth_qty: int | None = None


@dataclass(frozen=True)
class RankedContract:
    contract_symbol: str
    option_contract_id: uuid.UUID
    strike: float
    option_type: OptionType
    ltp: float
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)


def _nearest_strike_index(strikes: list[float], spot: float) -> int:
    return min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))


def _normalize(value: float, values: Sequence[float]) -> float:
    """0..1 min-max scaling; a single-valued set scores everything 1.0 rather
    than dividing by zero — "no spread in the data" shouldn't read as
    "every candidate is worst"."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return 1.0
    return (value - lo) / (hi - lo)


def _premium_fit_score(ltp: float, lo: float, hi: float) -> float:
    if lo <= ltp <= hi:
        return 1.0
    band = (hi - lo) if hi > lo else max(hi, 1.0)
    distance = (lo - ltp) if ltp < lo else (ltp - hi)
    return max(0.0, 1.0 - distance / band)


def _dte_allowed_strikes(
    distinct_strikes: list[float],
    atm_index: int,
    option_type: OptionType,
    dte: int,
    current_time: time,
    config: StrikeRankingConfig,
) -> set[float]:
    """Strike-index window for one option_type, given DTE/time-of-day —
    directional per the original spec ("ATM to 1-ITM", not a symmetric price
    window): for a call, ITM is the *lower*-index side; for a put, ITM is
    the *higher*-index side. All bounds are clamped to the actual strike
    list, same discipline `rank_strikes`' own default-window computation
    already used.
    """
    n = len(distinct_strikes)
    itm_is_lower = option_type == OptionType.CE

    if dte > 0:
        w = config.non_expiry_day_window
        lo, hi = max(0, atm_index - w), min(n - 1, atm_index + w)
        return set(distinct_strikes[lo : hi + 1])

    if current_time < config.expiry_day_afternoon_cutoff:
        w = config.expiry_morning_window
        if itm_is_lower:
            lo, hi = max(0, atm_index - w), atm_index
        else:
            lo, hi = atm_index, min(n - 1, atm_index + w)
        return set(distinct_strikes[lo : hi + 1])

    offset = config.expiry_afternoon_deep_itm_offset
    anchor = (atm_index - offset) if itm_is_lower else (atm_index + offset)
    anchor = max(0, min(n - 1, anchor))
    w = config.expiry_afternoon_window
    lo, hi = max(0, anchor - w), min(n - 1, anchor + w)
    return set(distinct_strikes[lo : hi + 1])


def _expiry_premium_floor(dte: int, current_time: time, config: StrikeRankingConfig) -> float:
    if dte == 0 and current_time < config.expiry_day_afternoon_cutoff:
        return config.expiry_morning_premium_floor
    return 0.0


def rank_strikes(
    underlying_spot: float,
    contracts: list[RankableContract],
    config: StrikeRankingConfig = StrikeRankingConfig(),
    *,
    dte: int | None = None,
    current_time: time | None = None,
) -> list[RankedContract]:
    """`dte`/`current_time` are keyword-only and both optional, deliberately
    unreachable by position — no existing caller can collide with them even
    by accident. DTE-awareness (the ATM/ITM/deep-ITM window rules from
    `StrikeRankingConfig`'s own docstring) only activates when *both* are
    supplied; passing just one falls back to the plain ATM±atm_range window
    rather than half-applying the new logic on incomplete information. This
    function still never reads the wall clock itself — `current_time` is
    always the caller's own injected value (`app.core.clock.now_ist().time()`
    for real use, a literal in tests) — preserving the "pure, no DB/broker/
    clock dependency" contract this module's own docstring already commits
    to.
    """
    if not contracts:
        return []

    distinct_strikes = sorted({c.strike for c in contracts})
    atm_index = _nearest_strike_index(distinct_strikes, underlying_spot)

    dte_aware = dte is not None and current_time is not None
    allowed_by_type: dict[OptionType, set[float]] | None = None
    shared_allowed: set[float] = set()
    premium_floor = 0.0
    if dte_aware:
        assert dte is not None and current_time is not None  # narrows for type checkers
        premium_floor = _expiry_premium_floor(dte, current_time, config)
        allowed_by_type = {
            option_type: _dte_allowed_strikes(
                distinct_strikes, atm_index, option_type, dte, current_time, config
            )
            for option_type in (OptionType.CE, OptionType.PE)
        }
    else:
        lo_idx = max(0, atm_index - config.atm_range)
        hi_idx = min(len(distinct_strikes) - 1, atm_index + config.atm_range)
        shared_allowed = set(distinct_strikes[lo_idx : hi_idx + 1])

    survivors: list[tuple[RankableContract, float]] = []
    for c in contracts:
        allowed = allowed_by_type[c.option_type] if allowed_by_type is not None else shared_allowed
        if c.strike not in allowed:
            continue
        if c.oi < config.min_oi or c.volume < config.min_volume:
            continue
        if c.ltp < premium_floor:
            continue
        mid = (c.bid + c.ask) / 2
        spread_pct = (c.ask - c.bid) / mid if mid > 0 else 1.0
        if spread_pct <= config.max_spread_pct:
            survivors.append((c, spread_pct))

    if not survivors:
        return []

    volumes = [c.volume for c, _ in survivors]
    ois = [c.oi for c, _ in survivors]
    spreads = [s for _, s in survivors]

    ranked: list[RankedContract] = []
    for c, spread_pct in survivors:
        spread_score = 1.0 - _normalize(spread_pct, spreads)
        volume_score = _normalize(c.volume, volumes)
        oi_score = _normalize(c.oi, ois)
        premium_fit_score = _premium_fit_score(
            c.ltp, config.preferred_premium_min, config.preferred_premium_max
        )
        depth_score = 0.5 if c.depth_qty is None else min(1.0, c.depth_qty / 1000.0)

        composite = (
            config.weight_spread * spread_score
            + config.weight_volume * volume_score
            + config.weight_oi * oi_score
            + config.weight_premium_fit * premium_fit_score
            + config.weight_depth * depth_score
        )
        ranked.append(
            RankedContract(
                contract_symbol=c.contract_symbol,
                option_contract_id=c.option_contract_id,
                strike=c.strike,
                option_type=c.option_type,
                ltp=c.ltp,
                score=round(composite, 6),
                breakdown={
                    "spread": round(spread_score, 4),
                    "volume": round(volume_score, 4),
                    "oi": round(oi_score, 4),
                    "premium_fit": round(premium_fit_score, 4),
                    "depth": round(depth_score, 4),
                },
            )
        )

    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def rank_from_latest_snapshot(
    db: Session,
    instrument_id: uuid.UUID,
    expiry_date: date,
    config: StrikeRankingConfig = StrikeRankingConfig(),
    *,
    dte: int | None = None,
    current_time: time | None = None,
) -> list[RankedContract]:
    """Loads the latest option-chain snapshot + underlying spot + best-effort
    depth for `instrument_id`/`expiry_date` and ranks it. Returns `[]` if
    there's no chain snapshot or no underlying tick yet — callers (the
    synthetic strategy stub, later real strategies) treat that as "nothing to
    trade this cycle", not an error.

    `dte`/`current_time` are forwarded to `rank_strikes` as-is — deliberately
    *not* defaulted here to a real `app.core.clock.now_ist()` read, even
    though this function is already a non-pure DB-loading wrapper (unlike
    `rank_strikes` itself). Doing so would silently change behavior for
    every existing caller (ORB/VWAP/EMA/OI-Vol/Sweep/Synthetic all call this
    positionally today) the moment DTE-awareness landed, rather than only
    for a caller that explicitly opts in.
    """
    from app.domain.market.models import DepthSnapshot as DepthSnapshotRow
    from app.domain.market.models import OptionChainSnapshot as OptionChainSnapshotRow
    from app.domain.market.models import OptionContract, QuoteTick

    latest_spot_tick = (
        db.query(QuoteTick)
        .filter(QuoteTick.instrument_id == instrument_id)
        .order_by(QuoteTick.ts.desc())
        .first()
    )
    if latest_spot_tick is None:
        return []

    latest_chain = (
        db.query(OptionChainSnapshotRow)
        .filter(
            OptionChainSnapshotRow.instrument_id == instrument_id,
            OptionChainSnapshotRow.expiry_date == expiry_date,
        )
        .order_by(OptionChainSnapshotRow.ts.desc())
        .first()
    )
    if latest_chain is None or not latest_chain.chain_data:
        return []

    symbols = [entry["contract_symbol"] for entry in latest_chain.chain_data]
    contract_rows = {
        row.symbol: row
        for row in db.query(OptionContract).filter(OptionContract.symbol.in_(symbols))
    }
    contract_ids = [row.id for row in contract_rows.values()]

    # Latest depth per contract, one query — kept simple/correct over
    # optimal; chain sizes here are the ATM±N window (tens of rows), not the
    # full chain, so an extra per-symbol round trip isn't the concern a
    # tick-level hot path would be.
    depth_qty_by_contract_id: dict[uuid.UUID, int] = {}
    if contract_ids:
        depth_rows = (
            db.query(DepthSnapshotRow)
            .filter(DepthSnapshotRow.option_contract_id.in_(contract_ids))
            .order_by(DepthSnapshotRow.option_contract_id, DepthSnapshotRow.ts.desc())
            .all()
        )
        seen: set[uuid.UUID] = set()
        for depth_row in depth_rows:
            if depth_row.option_contract_id in seen:
                continue
            seen.add(depth_row.option_contract_id)
            depth_qty_by_contract_id[depth_row.option_contract_id] = sum(
                level["qty"] for level in depth_row.bid_levels
            ) + sum(level["qty"] for level in depth_row.ask_levels)

    candidates: list[RankableContract] = []
    for entry in latest_chain.chain_data:
        contract_row = contract_rows.get(entry["contract_symbol"])
        if contract_row is None:
            continue
        candidates.append(
            RankableContract(
                contract_symbol=entry["contract_symbol"],
                option_contract_id=contract_row.id,
                strike=float(entry["strike"]),
                option_type=OptionType(entry["option_type"]),
                ltp=float(entry["ltp"]),
                bid=float(entry["bid"]),
                ask=float(entry["ask"]),
                volume=int(entry["volume"]),
                oi=int(entry["oi"]),
                depth_qty=depth_qty_by_contract_id.get(contract_row.id),
            )
        )

    return rank_strikes(
        float(latest_spot_tick.ltp), candidates, config, dte=dte, current_time=current_time
    )


def pick_top_by_type(
    ranked: list[RankedContract], option_type: OptionType
) -> RankedContract | None:
    """The highest-scored contract of a given `option_type` from an already
    `rank_strikes`-sorted list — the identical lookup ORB/VWAP Pullback/EMA
    Micro-pullback each did inline (`ranked` is already sorted best-first,
    so the first match is the top one). Not used by the synthetic strategy,
    which takes `ranked[0]` unconditionally regardless of type.
    """
    return next((r for r in ranked if r.option_type == option_type), None)
