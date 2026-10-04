import unittest
from backoff import retry_delay
from queueing import next_run


class RetryTests(unittest.TestCase):
    def test_first(self):
        self.assertEqual(retry_delay(0), 1)

    def test_exhausted(self):
        self.assertIsNone(retry_delay(4))

    def test_cap(self):
        self.assertEqual(retry_delay(10, max_attempts=20), 60)

    def test_explicit_zero(self):
        self.assertEqual(next_run(100, 503, 0, retry_after=0), 100)

    def test_rate_limit(self):
        self.assertEqual(next_run(100, 429, 0), 101)

    def test_permanent(self):
        self.assertIsNone(next_run(100, 501, 0))
