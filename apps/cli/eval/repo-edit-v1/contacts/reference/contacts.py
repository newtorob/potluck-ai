from csv_rows import parse_rows


def import_contacts(text):
    contacts = {}
    for row in parse_rows(text):
        email = (row.get('email') or '').strip().casefold()
        if email:
            contacts[email] = {'email': email, 'name': (row.get('name') or '').strip()}
    return list(contacts.values())
