"""Pure-logic tests for the strike-ranking engine — no DB/broker dependency,
mirroring test_indicators.py's approach for the indicator engine.
"""

from __future__ import annotations

import uuid

from app.domain.market.models import OptionType
from app.modules.strategy_engine.strike_ranking.engine import (
    RankableContract,
    StrikeRankingConfig,
    rank_strikes,
)


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


def test_ranked_output_sorted_descending_by_score():
    contracts = [
        _contract(22000, volume=100, oi=1000, ltp=500, option_type=OptionType.CE),
        _contract(22000, volume=5000, oi=50000, ltp=60, option_type=OptionType.PE),
    ]
    config = StrikeRankingConfig(atm_range=0, max_spread_pct=1.0)

    ranked = rank_strikes(22000.0, contracts, config)

    scores = [r.score for r in ranked]
    assert scores == sorted(scores, reverse=True)
