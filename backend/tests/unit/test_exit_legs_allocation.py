"""Pure-function coverage for `allocate_leg_lots_floored` — the "keep as many
legs as the lots allow, never under-fill" allocator behind the proportional
leg-collapse behaviour in `build_position_exit_legs`."""

from __future__ import annotations

from app.domain.strategy.exit_legs import allocate_leg_lots, allocate_leg_lots_floored


class TestAllocateLegLotsFloored:
    def test_one_lot_signals_collapse_to_single(self):
        assert allocate_leg_lots_floored(1, [0.5, 0.5]) == ([], [0, 1])
        assert allocate_leg_lots_floored(1, [0.4, 0.3, 0.3]) == ([], [0, 1, 2])

    def test_zero_lots_also_collapses(self):
        assert allocate_leg_lots_floored(0, [0.5, 0.5]) == ([], [0, 1])

    def test_fewer_lots_than_legs_drops_smallest_fractions(self):
        # 2 lots / 3 legs [.4,.3,.3] -> keep the .4 and the first .3, drop leg 2
        assert allocate_leg_lots_floored(2, [0.4, 0.3, 0.3]) == ([1, 1], [2])

    def test_fewer_lots_than_legs_four_way(self):
        # 2 lots / 4 legs [.3,.3,.2,.2] -> keep both .3 legs, drop both .2 legs
        assert allocate_leg_lots_floored(2, [0.3, 0.3, 0.2, 0.2]) == ([1, 1], [2, 3])

    def test_exactly_enough_lots_for_every_leg(self):
        assert allocate_leg_lots_floored(3, [0.4, 0.3, 0.3]) == ([1, 1, 1], [])
        assert allocate_leg_lots_floored(4, [0.3, 0.3, 0.2, 0.2]) == ([1, 1, 1, 1], [])

    def test_excess_lots_go_to_largest_fraction(self):
        # 6 / [.3,.3,.2,.2]: 1 each = 4, 2 excess -> the two .3 legs
        assert allocate_leg_lots_floored(6, [0.3, 0.3, 0.2, 0.2]) == ([2, 2, 1, 1], [])

    def test_full_ratio_when_lots_are_plentiful(self):
        assert allocate_leg_lots_floored(10, [0.4, 0.3, 0.3]) == ([4, 3, 3], [])

    def test_partial_ratio_forced_but_no_leg_zero(self):
        # 5 / [.4,.3,.3]: 1 each = 3; 2 excess; remainders .8,.6,.6 -> leg0,
        # then the .6 tie breaks toward the later leg (leg2), same rule as
        # allocate_leg_lots.
        assert allocate_leg_lots_floored(5, [0.4, 0.3, 0.3]) == ([2, 1, 2], [])

    def test_ties_break_toward_later_legs_for_excess(self):
        # 5 / [.34,.33,.33]: 1 each = 3, 2 excess; remainders .70,.65,.65
        #  -> leg0, then tie .65 -> later leg (leg2)
        assert allocate_leg_lots_floored(5, [0.34, 0.33, 0.33]) == ([2, 1, 2], [])

    def test_sum_always_equals_total_and_no_zero_survivor(self):
        for total in range(2, 40):
            for fracs in ([0.4, 0.3, 0.3], [0.3, 0.3, 0.2, 0.2], [0.5, 0.25, 0.25],
                          [0.6, 0.2, 0.1, 0.1], [0.2, 0.2, 0.2, 0.2, 0.2]):
                lots, dropped = allocate_leg_lots_floored(total, fracs)
                assert sum(lots) == total
                assert all(x >= 1 for x in lots)
                assert len(lots) == min(len(fracs), total)
                assert len(lots) + len(dropped) == len(fracs)
                assert dropped == sorted(dropped)

    def test_deterministic(self):
        for _ in range(50):
            assert allocate_leg_lots_floored(7, [0.45, 0.3, 0.25]) == \
                allocate_leg_lots_floored(7, [0.45, 0.3, 0.25])

    def test_matches_allocate_leg_lots_when_no_legs_dropped(self):
        # When k == len(fractions) the survivor allocation should agree with the
        # historical helper (both largest-remainder, ties toward later legs).
        for total, fracs in [(10, [0.3, 0.3, 0.4]), (8, [0.5, 0.25, 0.25]),
                             (11, [0.45, 0.3, 0.25])]:
            lots, dropped = allocate_leg_lots_floored(total, fracs)
            assert dropped == []
            assert lots == allocate_leg_lots(total, fracs)

    def test_single_leg_input_is_defensive_only(self):
        # build_position_exit_legs never calls this with <2 legs, but the
        # function should not blow up.
        assert allocate_leg_lots_floored(3, [1.0]) == ([3], [])
        assert allocate_leg_lots_floored(1, [1.0]) == ([], [0])


def test_pinned_allocate_leg_lots_is_unchanged():
    # Guard: the proportional-collapse work must not touch the historical helper.
    assert allocate_leg_lots(10, [0.3, 0.3, 0.4]) == [3, 3, 4]
    assert allocate_leg_lots(7, [0.3, 0.3, 0.4]) == [2, 2, 3]
    assert allocate_leg_lots(5, [0.34, 0.33, 0.33]) == [2, 1, 2]
    assert allocate_leg_lots(0, [0.5, 0.5]) == [0, 0]
