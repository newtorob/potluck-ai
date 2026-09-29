import unittest
from layers import merge_layers
from options import load_options


class IndependentTests(unittest.TestCase):
    def test_replacements(self):
        self.assertEqual(merge_layers({'a': {'b': 2}, 'x': [1]}, {'a': 0, 'x': []}), {'a': 0, 'x': []})
        self.assertEqual(merge_layers({'a': None}, {'a': {'b': ''}}), {'a': {'b': ''}})
        self.assertEqual(merge_layers(), {})

    def test_both_inputs_unchanged(self):
        a = {'x': {'left': [1]}}
        b = {'x': {'right': [2]}}
        out = merge_layers(a, b)
        out['x']['left'].append(8)
        out['x']['right'].append(9)
        self.assertEqual(a, {'x': {'left': [1]}})
        self.assertEqual(b, {'x': {'right': [2]}})

    def test_boundaries_and_defaults(self):
        self.assertEqual(load_options({}, {}, {}), {'workers': 1, 'timeout': 30})
        self.assertEqual(load_options({'workers': 64, 'timeout': 20}, {}, {'timeout': None}),
                         {'workers': 64, 'timeout': None})
        self.assertEqual(load_options({}, {}, {'timeout': 0.25, 'extra': ''})['extra'], '')

    def test_invalid_timeout(self):
        for value in (-0.01, False, '3', [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_options({}, {}, {'timeout': value})

    def test_float_workers_invalid(self):
        with self.assertRaises(ValueError):
            load_options({}, {}, {'workers': 2.0})
