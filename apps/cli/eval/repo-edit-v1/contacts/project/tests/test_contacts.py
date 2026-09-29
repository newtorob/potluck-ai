import unittest
from csv_rows import parse_rows
from contacts import import_contacts


class ContactsTests(unittest.TestCase):
    def test_quoted_comma(self):
        self.assertEqual(parse_rows('email,name\na@x,"Doe, Jo"\n'), [{'email': 'a@x', 'name': 'Doe, Jo'}])

    def test_empty(self):
        self.assertEqual(import_contacts(''), [])

    def test_normalization_and_latest(self):
        self.assertEqual(import_contacts('email,name\n A@X , First \nb@x,B\na@x,Last\n'),
                         [{'email': 'a@x', 'name': 'Last'}, {'email': 'b@x', 'name': 'B'}])

    def test_blank_email(self):
        self.assertEqual(import_contacts('email,name\n ,Nobody\n'), [])

    def test_missing_name(self):
        self.assertEqual(import_contacts('email\na@x\n'), [{'email': 'a@x', 'name': ''}])
