import unittest
from intervals import overlaps
from booking import is_available


class BookingTests(unittest.TestCase):
    def test_adjacent(self):
        self.assertFalse(overlaps((1, 3), (3, 5)))

    def test_empty(self):
        self.assertFalse(overlaps([2, 2], [1, 4]))

    def test_contained(self):
        self.assertTrue(overlaps([1, 5], [2, 4]))

    def test_empty_schedule(self):
        self.assertTrue(is_available([], (1, 2)))

    def test_one_conflict_blocks(self):
        self.assertFalse(is_available(iter([(0, 1), (3, 5)]), (4, 6)))

    def test_no_conflicts(self):
        self.assertTrue(is_available([(0, 1), (3, 5)], (1, 3)))
