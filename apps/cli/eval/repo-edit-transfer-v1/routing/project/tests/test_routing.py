import unittest
from paths import normalize_path
from routing import select_route


class RoutingTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize_path('api//items/?page=1#top'), '/api/items')
        self.assertEqual(normalize_path(''), '/')

    def test_longest(self):
        routes = [('/', 'root'), ('/api', 'api'), ('/api/items', 'items')]
        self.assertEqual(select_route('/api/items/5', routes), 'items')

    def test_boundary(self):
        self.assertIsNone(select_route('/apix', [('/api', 'wrong')]))

    def test_normalize_prefix(self):
        self.assertEqual(select_route('/api/x', [('api///', 'ok')]), 'ok')

    def test_tie(self):
        self.assertEqual(select_route('/api', [('/api', 'first'), ('/api/', 'last')]), 'first')
