def parse_rows(text):
    lines = text.splitlines()
    if not lines:
        return []
    header = lines[0].split(',')
    return [dict(zip(header, line.split(','))) for line in lines[1:]]
