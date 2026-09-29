from csv_rows import parse_rows


def import_contacts(text):
    seen = set()
    result = []
    for row in parse_rows(text):
        email = row['email'].lower()
        if email in seen:
            continue
        seen.add(email)
        result.append({'email': email, 'name': row['name']})
    return result
