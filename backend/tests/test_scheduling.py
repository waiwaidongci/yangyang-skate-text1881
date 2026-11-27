import unittest

from src.domain.scheduling import (CAPACITY_REASON, THRESHOLD_REASON, TIE_REASON, ZERO_HIT_REASON,
                                   PoolOrder, eligible_stations, schedule)

FULL = {'blade': 2, 'boot': 2, 'fastener': 2}


def order(ticket_id, scores, matched=3, gap=0.5, threshold=0.7):
    return PoolOrder(ticket_id=ticket_id, scores=scores, matched_ngrams=matched, gap=gap, threshold=threshold)


class EligibilityTest(unittest.TestCase):
    def test_zero_hit_stays_manual(self):
        result = schedule([order('A', {'blade': 0.9, 'boot': 0.06, 'fastener': 0.04}, matched=0)], FULL)
        self.assertEqual(result.assignments, {})
        self.assertEqual(result.manual, {'A': ZERO_HIT_REASON})

    def test_top_tie_stays_manual(self):
        result = schedule([order('A', {'blade': 0.5, 'boot': 0.5, 'fastener': 0.0}, gap=0.0)], FULL)
        self.assertEqual(result.assignments, {})
        self.assertEqual(result.manual, {'A': TIE_REASON})

    def test_below_threshold_stays_manual(self):
        result = schedule([order('A', {'blade': 0.6, 'boot': 0.3, 'fastener': 0.1})], FULL)
        self.assertEqual(result.assignments, {})
        self.assertEqual(result.manual, {'A': THRESHOLD_REASON})

    def test_any_station_over_threshold_is_eligible(self):
        entry = order('A', {'blade': 0.5, 'boot': 0.4, 'fastener': 0.1}, threshold=0.35)
        self.assertEqual(eligible_stations(entry), {'blade': 0.5, 'boot': 0.4})
        result = schedule([entry], {'blade': 0, 'boot': 1, 'fastener': 1})
        self.assertEqual(result.assignments, {'A': ('boot', 0.4)})


class AssignmentTest(unittest.TestCase):
    def test_cardinality_comes_before_score(self):
        orders = [
            order('A', {'blade': 0.9, 'boot': 0.8, 'fastener': 0.0}),
            order('B', {'blade': 0.85, 'boot': 0.1, 'fastener': 0.05}),
        ]
        result = schedule(orders, {'blade': 1, 'boot': 1, 'fastener': 0})
        self.assertEqual(result.assignments, {'A': ('boot', 0.8), 'B': ('blade', 0.85)})
        self.assertEqual(result.manual, {})

    def test_total_score_breaks_ties(self):
        orders = [
            order('A', {'blade': 0.9, 'boot': 0.7, 'fastener': 0.0}),
            order('B', {'blade': 0.6, 'boot': 0.65, 'fastener': 0.0}, threshold=0.6),
        ]
        result = schedule(orders, {'blade': 1, 'boot': 1, 'fastener': 0})
        self.assertEqual(result.assignments, {'A': ('blade', 0.9), 'B': ('boot', 0.65)})

    def test_capacity_shortage_goes_manual(self):
        orders = [
            order('A', {'blade': 0.9, 'boot': 0.06, 'fastener': 0.04}),
            order('B', {'blade': 0.8, 'boot': 0.1, 'fastener': 0.1}),
        ]
        result = schedule(orders, {'blade': 1, 'boot': 0, 'fastener': 0})
        self.assertEqual(result.assignments, {'A': ('blade', 0.9)})
        self.assertEqual(result.manual, {'B': CAPACITY_REASON})

    def test_zero_capacity(self):
        result = schedule([order('A', {'blade': 0.9, 'boot': 0.06, 'fastener': 0.04})],
                          {'blade': 0, 'boot': 0, 'fastener': 0})
        self.assertEqual(result.assignments, {})
        self.assertEqual(result.manual, {'A': CAPACITY_REASON})

    def test_shared_slots_pick_top_scores(self):
        orders = [order(f'T-{index}', {'blade': score, 'boot': 0.0, 'fastener': 0.0})
                  for index, score in enumerate((0.95, 0.9, 0.85, 0.8, 0.75))]
        result = schedule(orders, {'blade': 2, 'boot': 0, 'fastener': 0})
        self.assertEqual(result.assignments, {'T-0': ('blade', 0.95), 'T-1': ('blade', 0.9)})
        self.assertEqual(result.manual, {'T-2': CAPACITY_REASON, 'T-3': CAPACITY_REASON, 'T-4': CAPACITY_REASON})

    def test_highest_total_score_plan_wins(self):
        orders = [
            order('A', {'blade': 0.9, 'boot': 0.8, 'fastener': 0.0}),
            order('B', {'blade': 0.0, 'boot': 0.85, 'fastener': 0.0}),
            order('C', {'blade': 0.88, 'boot': 0.0, 'fastener': 0.0}),
        ]
        result = schedule(orders, {'blade': 1, 'boot': 1, 'fastener': 0})
        self.assertEqual(result.assignments, {'A': ('blade', 0.9), 'B': ('boot', 0.85)})
        self.assertEqual(result.manual, {'C': CAPACITY_REASON})

    def test_deterministic_repeat(self):
        orders = [
            order('A', {'blade': 0.9, 'boot': 0.8, 'fastener': 0.1}),
            order('B', {'blade': 0.7, 'boot': 0.75, 'fastener': 0.2}),
            order('C', {'blade': 0.4, 'boot': 0.4, 'fastener': 0.9}),
            order('D', {'blade': 0.85, 'boot': 0.1, 'fastener': 0.72}),
            order('E', {'blade': 0.6, 'boot': 0.88, 'fastener': 0.3}),
        ]
        capacities = {'blade': 2, 'boot': 1, 'fastener': 1}
        self.assertEqual(schedule(orders, capacities), schedule(orders, capacities))

    def test_empty_pool(self):
        self.assertEqual(schedule([], FULL), schedule([], FULL))
        result = schedule([], FULL)
        self.assertEqual(result.assignments, {})
        self.assertEqual(result.manual, {})


if __name__ == '__main__':
    unittest.main()
