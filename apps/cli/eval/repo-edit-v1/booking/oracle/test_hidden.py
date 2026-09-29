import copy
import unittest
from intervals import overlaps
from booking import is_available


class HiddenBookingTests(unittest.TestCase):
    def test_grid_against_point_sets(self):
        # Integer endpoints: doubled-grid membership gives an independent oracle.
        intervals = [(a, b) for a in range(-2, 4) for b in range(a, 4)]
        for a in intervals:
            for b in intervals:
                expected = bool(set(range(2*a[0], 2*a[1])) & set(range(2*b[0], 2*b[1])))
                for cast in (list, tuple):
                    with self.subTest(a=a, b=b, cast=cast.__name__):
                        self.assertEqual(overlaps(cast(a), cast(b)), expected)

    def test_schedule_and_immutability(self):
        bookings = [[-2, 0], [1, 3], [8, 8]]
        before = copy.deepcopy(bookings)
        for candidate, expected in [([0, 1], True), ([2, 2], True), ([-1, 2], False), ([3, 9], True)]:
            original = candidate[:]
            self.assertEqual(is_available(iter(bookings), candidate), expected)
            self.assertEqual(candidate, original)
            self.assertEqual(bookings, before)
