import unittest
from backoff import retry_delay
from queueing import next_run


class HiddenRetryTests(unittest.TestCase):
    def test_schedule(self):
        for i, expected in enumerate([1, 2, 4, 8, 16, 32, 60, 60]):
            self.assertEqual(retry_delay(i, 8), expected)
        self.assertIsNone(retry_delay(0, 0, 1))
        self.assertIsNone(retry_delay(10, 3, 0))

    def test_retry_after(self):
        for value, expected in [(0, 0), (0.5, 0.5), (1000, 60), (-3, 4), (None, 4)]:
            with self.subTest(value=value):
                self.assertEqual(retry_delay(2, retry_after=value), expected)

    def test_status_matrix(self):
        eligible = {408, 429, 500, 502, 503, 504}
        for status in range(100, 601):
            expected = 12.5 if status in eligible else None
            self.assertEqual(next_run(12, status, 1, retry_after=0.5), expected)
            self.assertIsNone(next_run(12, status, 4, retry_after=0))
