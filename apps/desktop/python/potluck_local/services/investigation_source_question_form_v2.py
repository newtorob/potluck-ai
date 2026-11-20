"""Conservative question syntax selects answer form, never truth or polarity."""
import re

VERSION = 'source-availability-form-v2'
_PREFIX = re.compile(
    r'^(?P<aux>Do|Does|do|does)\s+'
    r'(?P<determiner>the|these|those)\s+'
    r'(?P<subject>(?:[A-Za-z][A-Za-z-]*\s+){0,3}'
    r'(?P<head>note|notes|text|texts|document|documents|statement|statements|task|tasks))'
    r'\s+(?:not\s+)?(?P<predicate>specify|give|state|provide|list)\s+(?P<property>.+)$'
)
_UNSAFE_MODIFIERS = {
    'a', 'an', 'the', 'these', 'those', 'any', 'no', 'not', 'never',
    'in', 'on', 'at', 'by', 'for', 'from', 'with', 'without', 'to', 'of',
    'who', 'whom', 'whose', 'that', 'which', 'whether', 'and', 'or', 'but',
    'does', 'do', 'did', 'is', 'are', 'was', 'were', 'has', 'have',
}


def source_availability_frame(clause):
    if not isinstance(clause, str):
        return None
    candidate = clause.strip()
    # Ignore only a connective at a literal partition boundary. Payload text
    # remains untouched; no subject, negation, condition or property is removed.
    candidate = re.sub(r'^(?:,\s*)?(?:and\s+)', '', candidate)
    candidate = re.sub(r'[,;?\s]+$', '', candidate)
    candidate = re.sub(r'(?:,\s*)?\s+(?:and|or)$', '', candidate).rstrip(',; ')
    match = _PREFIX.fullmatch(candidate)
    if match is None:
        return None
    subject = match['subject'].split()
    if any(word.lower() in _UNSAFE_MODIFIERS for word in subject[:-1]):
        return None
    plural = match['head'] in {'notes', 'texts', 'documents', 'statements', 'tasks'}
    if (match['aux'].lower() == 'do') != plural:
        return None
    if match['determiner'] in {'these', 'those'} and not plural:
        return None
    complement = match['property']
    # Give/provide can describe permission or a task performing a service.
    # Restrict only clear information values, leaving other readings open.
    if match['predicate'] in {'give', 'provide'}:
        if (match['head'] in {'task', 'tasks'}
                or not re.search(r'\b(?:time|timing|duration|date|deadline|size|value|total|fee|term|count|number|limit|minimum|maximum)\b', complement, re.I)
                or re.search(r'\b(?:permission|access|consent|authorization|right|exemption|service)\b', complement, re.I)):
            return None
    # Compound, quoted and conditional scopes require the unrestricted model
    # contract. This intentionally recognizes less than full English grammar.
    if re.search(r'[,;\n"“”`]|\b(?:and|or|but|while|whereas|unless|if)\b', complement, re.I):
        return None
    return {
        'version': VERSION,
        'answer_form': 'selected_text_availability_yes_no',
        'basis': 'literal document subject and content predicate',
        'question_only': True,
        'answer_or_polarity_inferred': False,
    }
