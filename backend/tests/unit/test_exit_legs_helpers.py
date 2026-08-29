"""Pure-function coverage for the multi-leg exit engine's domain helpers
(`app.domain.strategy.exit_legs`) — no DB, no broker."""

from __future__ import annotations

import pytest

from app.domain.strategy.exit_legs import (
    ExitLegTemplate,
    allocate_leg_lots,
    build_exit_legs,
    deserialize_exit_leg_templates,
    deserialize_exit_legs,
    serialize_exit_legs,
    validate_exit_leg_templates,
)


class TestAllocateLegLots:
    def test_clean_split(self):
        assert allocate_leg_lots(10, [0.3, 0.3, 0.4]) == [3, 3, 4]

    def test_largest_remainder_goes_to_biggest_fraction(self):
        # 7 * [.3,.3,.4] = [2.1, 2.1, 2.8]; floors sum 6; 1 left → to the .8
        assert allocate_leg_lots(7, [0.3, 0.3, 0.4]) == [2, 2, 3]

    def test_two_leftover_lots_spread_by_fraction_then_later_leg(self):
        # 8 * [.5,.25,.25] = [4,2,2] exact
        assert allocate_leg_lots(8, [0.5, 0.25, 0.25]) == [4, 2, 2]
        # 5 * [.34,.33,.33] = [1.7,1.65,1.65]; floors [1,1,1]=3; 2 left →
        # fracs .7,.65,.65 → leg0 then (tie .65) later leg wins → leg2
        assert allocate_leg_lots(5, [0.34, 0.33, 0.33]) == [2, 1, 2]

    def test_deterministic(self):
        for _ in range(50):
            assert allocate_leg_lots(11, [0.45, 0.3, 0.25]) == allocate_leg_lots(
                11, [0.45, 0.3, 0.25]
            )

    def test_zero_total(self):
        assert allocate_leg_lots(0, [0.5, 0.5]) == [0, 0]


class TestValidateTemplates:
    def _t(self, frac, **kw):
        return ExitLegTemplate(qty_fraction=frac, **kw)

    def test_ok(self):
        validate_exit_leg_templates([self._t(0.3), self._t(0.3), self._t(0.4)])

    def test_fractions_must_sum_to_one(self):
        with pytest.raises(ValueError, match="sum to 1.0"):
            validate_exit_leg_templates([self._t(0.3), self._t(0.3)])

    def test_at_least_two_legs(self):
        with pytest.raises(ValueError, match="at least 2"):
            validate_exit_leg_templates([self._t(1.0)])

    def test_at_most_six_legs(self):
        with pytest.raises(ValueError, match="at most 6"):
            validate_exit_leg_templates([self._t(1 / 7) for _ in range(7)])

    def test_fraction_range(self):
        with pytest.raises(ValueError, match="qty_fraction"):
            validate_exit_leg_templates([self._t(-0.1), self._t(1.1)])

    def test_no_target_conflicts_with_target_pct(self):
        with pytest.raises(ValueError, match="no_target and target_pct"):
            validate_exit_leg_templates(
                [self._t(0.5, no_target=True, target_pct=0.3), self._t(0.5)]
            )

    def test_stop_pct_range(self):
        with pytest.raises(ValueError, match="stop_pct"):
            validate_exit_leg_templates([self._t(0.5, stop_pct=1.5), self._t(0.5)])


class TestDeserializeTemplates:
    def test_tolerates_unknown_keys(self):
        out = deserialize_exit_leg_templates(
            [{"qty_fraction": 0.5, "future_key": 1}, {"qty_fraction": 0.5}]
        )
        assert out is not None and len(out) == 2 and out[0].qty_fraction == 0.5

    def test_none_passthrough(self):
        assert deserialize_exit_leg_templates(None) is None

    def test_rejects_non_list(self):
        with pytest.raises(ValueError, match="must be a list"):
            deserialize_exit_leg_templates({"qty_fraction": 1.0})

    def test_rejects_missing_qty_fraction(self):
        with pytest.raises(ValueError, match="invalid exit leg"):
            deserialize_exit_leg_templates([{"kind": "x"}])


class TestBuildExitLegs:
    def test_base_prices_used_when_leg_pct_absent(self):
        specs = build_exit_legs(
            [ExitLegTemplate(0.5), ExitLegTemplate(0.5)],
            entry_price=100.0,
            base_stop_price=82.0,
            base_target_price=118.0,
            tick_size=0.05,
        )
        assert [s.stop_price for s in specs] == [82.0, 82.0]
        assert [s.target_price for s in specs] == [118.0, 118.0]

    def test_per_leg_pct_overrides_and_runner(self):
        specs = build_exit_legs(
            [
                ExitLegTemplate(0.3, kind="fixed_sl"),
                ExitLegTemplate(0.3, kind="sr_target", target_pct=0.15),
                ExitLegTemplate(0.4, kind="runner", no_target=True),
            ],
            entry_price=100.0,
            base_stop_price=82.0,
            base_target_price=118.0,
            tick_size=0.05,
        )
        assert specs[0].target_price == 118.0  # base
        assert specs[1].target_price == 115.0  # 100 * 1.15
        assert specs[2].target_price is None  # runner
        assert all(s.stop_price == 82.0 for s in specs)

    def test_use_structure_flag_carries_the_trio(self):
        specs = build_exit_legs(
            [ExitLegTemplate(0.5, use_structure=True), ExitLegTemplate(0.5)],
            entry_price=100.0,
            base_stop_price=82.0,
            base_target_price=118.0,
            tick_size=0.05,
            structure_level=24000.0,
            structure_break_buffer=5.0,
            structure_break_persistence_seconds=6.0,
        )
        assert specs[0].structure_level == 24000.0
        assert specs[0].structure_break_buffer == 5.0
        assert specs[1].structure_level is None


def test_serialize_roundtrip():
    specs = build_exit_legs(
        [ExitLegTemplate(0.4), ExitLegTemplate(0.6, no_target=True)],
        entry_price=50.0,
        base_stop_price=41.0,
        base_target_price=59.0,
        tick_size=0.05,
    )
    raw = serialize_exit_legs(specs)
    assert raw is not None
    back = deserialize_exit_legs(raw)
    assert back == specs


def test_serialize_none():
    assert serialize_exit_legs(None) is None
    assert serialize_exit_legs([]) is None
