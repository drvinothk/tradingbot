"""Pure-logic tests for the strike-ranking engine — no DB/broker dependency,
mirroring test_indicators.py's approach for the indicator engine.
"""

from __future__ import annotations

import uuid
from datetime import time

from app.domain.market.models import OptionType
from app.modules.strategy_engine.strike_ranking.engine import (
    RankableContract,
    StrikeRankingConfig,
    rank_strikes,
)

# Wide, evenly-spaced strike ladder shared by every DTE-aware test below —
# atm_index=5 (spot=22000) with enough strikes on both sides that the
# afternoon deep-ITM offset (default 3) never clips against the edge of the
# list, which a narrower ladder would silently do and make the test not
# actually prove what it claims to.
_DTE_STRIKES = [21500, 21600, 21700, 21800, 21900, 22000, 22100, 22200, 22300, 22400, 22500]


def _contract(
    strike: float,
    *,
    ltp: float = 80.0,
    bid: float | None = None,
    ask: float | None = None,
    volume: int = 1000,
    oi: int = 10000,
    option_type: OptionType = OptionType.CE,
    depth_qty: int | None = None,
) -> RankableContract:
    spread = 0.5
    return RankableContract(
        contract_symbol=f"NIFTY-{strike}-{option_type.value}",
        option_contract_id=uuid.uuid4(),
        strike=strike,
        option_type=option_type,
        ltp=ltp,
        bid=bid if bid is not None else ltp - spread,
        ask=ask if ask is not None else ltp + spread,
        volume=volume,
        oi=oi,
        depth_qty=depth_qty,
    )


def test_empty_contracts_returns_empty():
    assert rank_strikes(22000.0, []) == []


def test_atm_window_selects_strikes_by_index_not_price_distance():
    # 50-point steps around spot 22000 (ATM). atm_range=1 -> only 21950/22000/22050.
    strikes = [21900, 21950, 22000, 22050, 22100]
    contracts = [_contract(s) for s in strikes]
    config = StrikeRankingConfig(atm_range=1, max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, contracts, config)

    ranked_strikes = {r.strike for r in ranked}
    assert ranked_strikes == {21950, 22000, 22050}


def test_wide_spread_is_hard_filtered_out():
    tight = _contract(22000, ltp=80.0, bid=79.5, ask=80.5)  # ~1.25% spread
    wide = _contract(22050, ltp=80.0, bid=60.0, ask=100.0)  # 50% spread
    config = StrikeRankingConfig(atm_range=2, max_spread_pct=0.05)

    ranked = rank_strikes(22000.0, [tight, wide], config)

    assert [r.strike for r in ranked] == [22000]


def test_higher_volume_and_oi_score_higher_all_else_equal():
    # Same strike, different option_type — both survive the single-strike
    # ATM window (atm_range=0) as distinct rows.
    low = _contract(22000, volume=100, oi=1000, option_type=OptionType.CE)
    high = _contract(22000, volume=5000, oi=50000, option_type=OptionType.PE)
    config = StrikeRankingConfig(atm_range=0, max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, [low, high], config)

    assert ranked[0].option_type == OptionType.PE
    assert ranked[0].score > ranked[1].score


def test_premium_fit_prefers_contracts_inside_preferred_band():
    inside = _contract(22000, ltp=60.0, option_type=OptionType.CE)
    outside = _contract(22050, ltp=500.0, option_type=OptionType.CE)
    config = StrikeRankingConfig(
        atm_range=2, max_spread_pct=1.0, preferred_premium_min=20.0, preferred_premium_max=150.0
    )

    ranked = rank_strikes(22000.0, [inside, outside], config)

    breakdown_by_strike = {r.strike: r.breakdown["premium_fit"] for r in ranked}
    assert breakdown_by_strike[22000] == 1.0
    assert breakdown_by_strike[22050] < 1.0


def test_missing_depth_scores_neutral_not_zero():
    contract = _contract(22000, depth_qty=None)
    config = StrikeRankingConfig(atm_range=0, max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, [contract], config)

    assert ranked[0].breakdown["depth"] == 0.5


def test_min_oi_min_volume_default_to_zero_and_filter_nothing():
    # Default StrikeRankingConfig() has min_oi=min_volume=0 — every existing
    # strategy (ORB/VWAP/EMA/Synthetic) relies on this not filtering
    # anything out, so this pins that default explicitly.
    contract = _contract(22000, volume=1, oi=1)
    config = StrikeRankingConfig(atm_range=0, max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, [contract], config)

    assert [r.strike for r in ranked] == [22000]


def test_below_participation_floor_is_hard_filtered_out():
    thin = _contract(22000, volume=100, oi=1000)
    liquid = _contract(22050, volume=5000, oi=50000)
    config = StrikeRankingConfig(
        atm_range=2, max_spread_pct=1.0, min_oi=5000, min_volume=500
    )

    ranked = rank_strikes(22000.0, [thin, liquid], config)

    assert [r.strike for r in ranked] == [22050]


def test_ranked_output_sorted_descending_by_score():
    contracts = [
        _contract(22000, volume=100, oi=1000, ltp=500, option_type=OptionType.CE),
        _contract(22000, volume=5000, oi=50000, ltp=60, option_type=OptionType.PE),
    ]
    config = StrikeRankingConfig(atm_range=0, max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, contracts, config)

    scores = [r.score for r in ranked]
    assert scores == sorted(scores, reverse=True)


# -- Ops-Hardening Phase 1: DTE-aware strike windows -------------------------


def test_dte_and_current_time_omitted_preserves_plain_atm_window():
    # Sanity pin: calling exactly as before (no dte/current_time at all)
    # must be byte-for-byte the old behavior -- this is what makes every
    # existing caller/test above safe to leave untouched.
    contracts = [_contract(s, option_type=OptionType.CE) for s in _DTE_STRIKES]
    config = StrikeRankingConfig(atm_range=1, max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, contracts, config)

    assert {r.strike for r in ranked} == {21900, 22000, 22100}


def test_supplying_only_one_of_dte_or_current_time_falls_back_to_plain_window():
    # A partial/mistaken call must not half-apply DTE logic -- it silently
    # degrades to the well-tested plain-window path instead.
    contracts = [_contract(s, option_type=OptionType.CE) for s in _DTE_STRIKES]
    config = StrikeRankingConfig(atm_range=1, max_spread_pct=1.0)

    ranked_dte_only = rank_strikes(22000.0, contracts, config, dte=1)
    ranked_time_only = rank_strikes(22000.0, contracts, config, current_time=time(10, 0))

    assert {r.strike for r in ranked_dte_only} == {21900, 22000, 22100}
    assert {r.strike for r in ranked_time_only} == {21900, 22000, 22100}


def test_non_expiry_day_window_is_symmetric_atm_plus_minus_one():
    contracts = [_contract(s, option_type=OptionType.CE) for s in _DTE_STRIKES]
    config = StrikeRankingConfig(max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, contracts, config, dte=1, current_time=time(10, 0))

    assert {r.strike for r in ranked} == {21900, 22000, 22100}


def test_expiry_morning_window_is_itm_only_for_calls():
    # Calls: ITM is the lower-strike side. "ATM to 1-ITM" must exclude the
    # OTM strike one above ATM (22100), even though it's the same distance
    # the non-expiry-day window would have included.
    contracts = [_contract(s, option_type=OptionType.CE) for s in _DTE_STRIKES]
    config = StrikeRankingConfig(max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, contracts, config, dte=0, current_time=time(10, 0))

    assert {r.strike for r in ranked} == {21900, 22000}


def test_expiry_morning_window_is_itm_only_for_puts():
    # Puts: ITM is the higher-strike side -- the mirror image of the call
    # case above, proving the window is genuinely per-option-type, not a
    # single shared price band.
    contracts = [_contract(s, option_type=OptionType.PE) for s in _DTE_STRIKES]
    config = StrikeRankingConfig(max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, contracts, config, dte=0, current_time=time(10, 0))

    assert {r.strike for r in ranked} == {22000, 22100}


def test_expiry_morning_premium_floor_hard_filters_below_threshold():
    cheap = _contract(21900, ltp=30.0, option_type=OptionType.CE)  # in-window, below floor
    rich = _contract(22000, ltp=80.0, option_type=OptionType.CE)  # in-window, above floor
    config = StrikeRankingConfig(max_spread_pct=1.0, expiry_morning_premium_floor=50.0)

    ranked = rank_strikes(22000.0, [cheap, rich], config, dte=0, current_time=time(10, 0))

    assert [r.strike for r in ranked] == [22000]


def test_expiry_morning_premium_floor_can_empty_result_without_crashing():
    # The regression this directly guards against: if every in-window
    # candidate is below the floor, rank_strikes must return [] cleanly,
    # not raise or return None -- exactly what every real call site
    # (pick_top_by_type / `if not ranked`) already expects.
    contracts = [
        _contract(21900, ltp=10.0, option_type=OptionType.CE),
        _contract(22000, ltp=20.0, option_type=OptionType.CE),
    ]
    config = StrikeRankingConfig(max_spread_pct=1.0, expiry_morning_premium_floor=50.0)

    ranked = rank_strikes(22000.0, contracts, config, dte=0, current_time=time(10, 0))

    assert ranked == []


def test_expiry_afternoon_window_anchors_on_deep_itm_excluding_atm():
    # Calls: deep ITM is well below spot. Default offset=3 from atm_index=5
    # (see _DTE_STRIKES) anchors on 21700, well clear of the ATM strike
    # (22000) itself -- "avoid theta decay traps" means genuinely away from
    # the money, not just one strike over like the morning window.
    contracts = [_contract(s, option_type=OptionType.CE) for s in _DTE_STRIKES]
    config = StrikeRankingConfig(max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, contracts, config, dte=0, current_time=time(14, 0))

    ranked_strikes = {r.strike for r in ranked}
    assert ranked_strikes == {21600, 21700, 21800}
    assert 22000 not in ranked_strikes


def test_expiry_afternoon_window_mirrors_for_puts():
    contracts = [_contract(s, option_type=OptionType.PE) for s in _DTE_STRIKES]
    config = StrikeRankingConfig(max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, contracts, config, dte=0, current_time=time(14, 0))

    ranked_strikes = {r.strike for r in ranked}
    assert ranked_strikes == {22200, 22300, 22400}
    assert 22000 not in ranked_strikes


def test_expiry_afternoon_has_no_premium_floor_by_default():
    # The floor is scoped to the morning rule only, per the original spec --
    # a cheap deep-ITM contract in the afternoon window must still survive.
    contracts = [_contract(21700, ltp=5.0, option_type=OptionType.CE)]
    config = StrikeRankingConfig(max_spread_pct=1.0, expiry_morning_premium_floor=50.0)

    ranked = rank_strikes(22000.0, contracts, config, dte=0, current_time=time(14, 0))

    assert [r.strike for r in ranked] == [21700]
