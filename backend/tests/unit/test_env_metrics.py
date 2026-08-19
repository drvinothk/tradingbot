"""strategy_engine.env_metrics.compute_pcr -- pure aggregation logic, no DB.
DB-backed behavior (`get_env_metrics`/`get_latest_env_metrics`, VIX lookup,
option-chain-snapshot lookup, `as_of_utc` historical reconstruction) is
covered in tests/integration/test_env_metrics.py.
"""

from __future__ import annotations

from app.modules.strategy_engine.env_metrics import compute_pcr


def _entry(option_type: str, oi: int | None, volume: int | None) -> dict:
    return {"option_type": option_type, "oi": oi, "volume": volume}


def test_empty_chain_returns_none_for_both_ratios():
    assert compute_pcr([]) == (None, None)


def test_all_call_side_zero_put_gives_zero_ratios_not_none():
    chain = [_entry("CE", oi=1000, volume=500)]
    pcr_oi, pcr_vol = compute_pcr(chain)
    assert pcr_oi == 0.0
    assert pcr_vol == 0.0


def test_all_put_side_zero_call_gives_none_ratios_divide_by_zero_guard():
    chain = [_entry("PE", oi=1000, volume=500)]
    assert compute_pcr(chain) == (None, None)


def test_mixed_chain_computes_summed_put_over_call_ratio():
    chain = [
        _entry("CE", oi=1000, volume=200),
        _entry("CE", oi=500, volume=100),
        _entry("PE", oi=750, volume=150),
        _entry("PE", oi=750, volume=150),
    ]
    pcr_oi, pcr_vol = compute_pcr(chain)
    assert pcr_oi == (750 + 750) / (1000 + 500)
    assert pcr_vol == (150 + 150) / (200 + 100)


def test_none_oi_and_volume_entries_treated_as_zero_not_a_crash():
    chain = [_entry("CE", oi=100, volume=None), _entry("PE", oi=None, volume=None)]
    pcr_oi, pcr_vol = compute_pcr(chain)
    assert pcr_oi == 0.0
    assert pcr_vol is None  # both call and put volume are 0 -> zero denominator


def test_entries_with_unrecognized_option_type_are_ignored():
    chain = [_entry("CE", oi=100, volume=50), _entry("FUT", oi=99999, volume=99999)]
    pcr_oi, pcr_vol = compute_pcr(chain)
    assert pcr_oi == 0.0
    assert pcr_vol == 0.0
