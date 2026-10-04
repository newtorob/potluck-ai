import unittest
from layers import merge_layers
from options import load_options


class OptionsTests(unittest.TestCase):
    def test_recursive(self):
        self.assertEqual(merge_layers({'db': {'host': 'a', 'port': 1}}, {'db': {'port': 2}}),
                         {'db': {'host': 'a', 'port': 2}})

    def test_precedence(self):
        self.assertEqual(load_options({'workers': 2}, {'workers': 3}, {'workers': 4})['workers'], 4)

    def test_explicit_values(self):
        self.assertEqual(merge_layers({'x': 1}, {'x': None, 'flag': False}), {'x': None, 'flag': False})
        self.assertEqual(load_options({}, {}, {'timeout': 0})['timeout'], 0)

    def test_isolation(self):
        original = {'items': [{'x': 1}]}
        result = merge_layers(original)
        result['items'][0]['x'] = 5
        self.assertEqual(original['items'][0]['x'], 1)

    def test_invalid(self):
        for value in (0, 65, True, '2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_options({}, {}, {'workers': value})
