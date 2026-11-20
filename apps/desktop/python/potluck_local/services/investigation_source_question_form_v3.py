"""Question-only source description syntax; never asserts facts or polarity."""
import re
from .investigation_source_question_form_v2 import source_availability_frame

VERSION = 'source-description-form-v1'
_PREFIX = re.compile(r'\A\s*(?:(?:,\s*)?(?:and\s+)|(?:if\s+not\s*,\s*))?', re.I)
_FRAME = re.compile(
    r'what\s+(?P<properties>[A-Za-z][A-Za-z0-9 -]*)\s+'
    r'(?P<aux>do|does)\s+(?P<subject>they|(?:the|these|those)\s+'
    r'(?:[A-Za-z][A-Za-z-]*\s+){0,3}(?:note|notes|task|tasks|document|documents|statement|statements|text|texts))'
    r'\s+(?:describe|state|list)\s*\Z', re.I)
_UNSAFE = frozenset({'if', 'unless', 'when', 'while', 'whether', 'not', 'no', 'never',
                     'without', 'except', 'or', 'but', 'who', 'which', 'that', 'what',
                     'can', 'could', 'may', 'might', 'must', 'shall', 'should', 'will',
                     'would', 'did', 'does', 'do', 'is', 'are', 'was', 'were', 'has',
                     'have', 'had', 'performed', 'happened', 'occurred', 'completed',
                     'permission', 'authorization', 'consent', 'permit', 'allow'})


def _match(clause, parent_question=None):
    if not isinstance(clause, str):
        return None
    start = _PREFIX.match(clause).end()
    candidate = re.sub(r'[,;?\s]+$', '', clause[start:])
    match = _FRAME.fullmatch(candidate)
    if not match:
        return None
    subject = match['subject'].lower().split()
    if subject != ['they']:
        if any(word in _UNSAFE or word in {'and', 'or', 'the', 'these', 'those'} for word in subject[1:-1]):
            return None
        plural = subject[-1] in {'notes', 'tasks', 'documents', 'statements', 'texts'}
        if (match['aux'].lower() == 'do') != plural or subject[0] in {'these', 'those'} and not plural:
            return None
    elif (match['aux'].lower() != 'do' or not isinstance(parent_question, str)
          or parent_question.count(clause) != 1):
        return None
    words = match['properties'].lower().split()
    if any(word in _UNSAFE for word in words) or words.count('and') > 1:
        return None
    if 'and' in words:
        at = words.index('and')
        if not 1 <= at <= 4 or not 1 <= len(words) - at - 1 <= 4:
            return None
    elif not 1 <= len(words) <= 4:
        return None
    return start, match


def description_request_frame(clause, parent_question=None):
    matched = _match(clause, parent_question)
    if matched is None:
        return None
    subject = matched[1]['subject'].lower()
    unresolved = subject == 'they'
    return {'version': VERSION, 'target': 'reported_description', 'question_only': True,
            'subject_reference': 'unresolved_pronoun' if unresolved else 'explicit_document_subject',
            'source_reference_resolved': not unresolved,
            'known_or_truth_inferred': False, 'answer_or_polarity_inferred': False,
            'parent_question': parent_question}


def source_description_frame(clause, parent_question=None):
    matched = _match(clause, parent_question)
    if matched is None or matched[1]['subject'].lower() == 'they':
        return None
    return {'version': VERSION, 'target': 'description', 'question_only': True,
            'known_or_truth_inferred': False, 'answer_or_polarity_inferred': False,
            'parent_question': parent_question}


def _refine(clause, matched):
    if matched is None:
        return [clause]
    start, match = matched
    conjunction = list(re.finditer(r'\s+and\s+', match['properties'], re.I))
    if len(conjunction) != 1:
        return [clause]
    boundary = start + match.start('properties') + conjunction[0].start()
    return [clause[:boundary], clause[boundary:]]


def refine_description_request_clause(clause, parent_question=None):
    return _refine(clause, _match(clause, parent_question))


def refine_source_description_clause(clause, parent_question=None):
    matched = _match(clause, parent_question)
    if matched is not None and matched[1]['subject'].lower() == 'they':
        return [clause]
    return _refine(clause, matched)
