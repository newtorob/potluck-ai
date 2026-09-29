import csv
import io


def parse_rows(text):
    return list(csv.DictReader(io.StringIO(text, newline='')))
