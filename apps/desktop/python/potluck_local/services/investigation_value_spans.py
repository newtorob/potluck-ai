"""Bound literal value spans; source binding is not semantic verification."""
from copy import deepcopy
import re

from . import investigation_evidence_comparisons as source_contract

MAX_TYPE_QUOTE_CHARS = 240
VALUE = re.compile(r'(?:[vV]?[0-9]+(?:\.[0-9]+)*(?:[-+][A-Za-z0-9.]+)?|[A-Za-z][A-Za-z0-9_-]*[0-9][A-Za-z0-9_-]*)\Z')
BOUNDARY = re.compile(r'[A-Za-z0-9_./\\:@%+~#?=&-]')


def request_scope(prepared):
    focus = prepared['selection_scope']['focus']
    if re.search(r'\b(?:versions?|releases?)\b', focus, re.I): return 'release_version'
    if re.search(r'\b(?:identifiers?|ids?)\b', focus, re.I): return 'entity_identifier'
    return 'outside_scope'



def _fact(row, unit):
    value, quote, text = row['value'], row['type_quote'], unit['quote']
    if (not isinstance(value, str) or not 1 <= len(value) <= 64 or not VALUE.fullmatch(value)
            or not isinstance(quote, str) or not 1 <= len(quote) <= MAX_TYPE_QUOTE_CHARS
            or not isinstance(text, str) or not re.search(r'[A-Za-z]', quote.replace(value, ''))):
        raise ValueError('Invalid value or type quote')
    if (re.fullmatch(r'(?:sha|md5|sha1|sha256|sha512)[-_]?[0-9]*', value, re.I)
            or re.fullmatch(r'[a-fA-F0-9]{16,}', value)):
        raise ValueError('Hash labels are not identifiers')
    if value.isdigit() and re.search(r'\b(?:http\s+)?(?:status|error\s+code|acknowledg(?:e)?ment|ack)\b', quote, re.I):
        raise ValueError('Status codes are not version values')
    matches = []
    for qm in re.finditer(re.escape(quote), text):
        for vm in re.finditer(re.escape(value), quote):
            a, b = qm.start() + vm.start(), qm.start() + vm.end()
            left_blocked = a and BOUNDARY.fullmatch(text[a-1])
            right_blocked = b < len(text) and BOUNDARY.fullmatch(text[b])
            # A sentence-ending period does not extend an identifier or version.
            if right_blocked and text[b] == '.' and (b+1 == len(text) or text[b+1].isspace() or text[b+1] in '"”’)]}'):
                right_blocked = False
            if left_blocked or right_blocked:
                continue
            matches.append((qm.start(), qm.end(), a, b))
    if len(matches) != 1:
        raise ValueError('Value and type quote must have one exact token binding')
    a, b, c, d = matches[0]
    return {**deepcopy(row), 'type_quote_start': a, 'type_quote_end': b, 'value_start': c, 'value_end': d}


