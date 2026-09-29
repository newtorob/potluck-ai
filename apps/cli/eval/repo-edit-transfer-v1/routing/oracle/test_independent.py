import unittest
from paths import normalize_path
from routing import select_route


class IndependentTests(unittest.TestCase):
    def test_literal_segments(self):
        self.assertEqual(normalize_path('///A/%2f/./../'), '/A/%2f/./..')
        self.assertEqual(normalize_path('/a#frag?query'), '/a')
        self.assertEqual(normalize_path('?query'), '/')

    def test_root(self):
        self.assertEqual(select_route('x', [('', 'root')]), 'root')
        self.assertIsNone(select_route('/', []))

    def test_identity_and_no_mutation(self):
        target = []
        routes = [('/X', target), ('/', object())]
        before = list(routes)
        self.assertIs(select_route('/X/y', routes), target)
        self.assertEqual(routes, before)

    def test_ties_use_normalized_length(self):
        self.assertEqual(select_route('/x/y', [('/x//', 'first'), ('/x', 'second')]), 'first')
        self.assertEqual(select_route('/x/y/z', [('/x////', 'short'), ('/x/y', 'long')]), 'long')

    def test_case_and_encoded_boundary(self):
        self.assertIsNone(select_route('/X', [('/x', 'wrong')]))
        self.assertIsNone(select_route('/api%2fx', [('/api', 'wrong')]))
