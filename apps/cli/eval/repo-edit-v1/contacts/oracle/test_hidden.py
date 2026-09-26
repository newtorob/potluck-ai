import unittest
from csv_rows import parse_rows
from contacts import import_contacts


class HiddenContactsTests(unittest.TestCase):
    def test_multiline_and_quotes(self):
        text = 'email,name,note\r\na@x," A, ""B""\nC ",ignored\r\n'
        self.assertEqual(parse_rows(text)[0]['name'], ' A, "B"\nC ')
        self.assertEqual(import_contacts(text), [{'email': 'a@x', 'name': 'A, "B"\nC'}])

    def test_missing_fields(self):
        for text in ('name\nNobody\n', 'email,name\n,Empty\n'):
            self.assertEqual(import_contacts(text), [])
        self.assertEqual(import_contacts('email,name\na@x\n'), [{'email': 'a@x', 'name': ''}])

    def test_casefold_and_order(self):
        text = 'email,name\n Straße@X ,One\nZ@x,Zed\nSTRASSE@x,Two\nZ@x,Zee\n'
        original = text[:]
        self.assertEqual(import_contacts(text), [{'email': 'strasse@x', 'name': 'Two'}, {'email': 'z@x', 'name': 'Zee'}])
        self.assertEqual(text, original)

    def test_header_only(self):
        self.assertEqual(import_contacts('email,name\r\n'), [])
