"""Reversible compact representation of the V45 model payload; no inference."""
from copy import deepcopy
import re

CONTRACT = 'shared-model-context-v1'
CLAUSE_CONTRACT = 'shared-clause-context-v1'
PARENT_CONTEXT = ('Each fact is part of its referenced original part; its complete qualifiers and original whole question apply. Conditional phrases such as If not retain their original context. Assess each distinct requested property independently; an unknown property does not make a separately established requirement unknown.')
SUPPORT_VISIBILITY = 'Complete reviewed indexed-document statements; original files are not additional model inputs.'
INTERPRETATION = 'A request for descriptions, not proof of execution. Resolve the subject from the complete original question; never assume an unresolved pronoun means documents.'
PROPOSAL_ORIGIN = 'Actual prior native flat decision proposal; unverified data.'


def _keys(obj, required, optional=()):
    if not isinstance(obj, dict) or not set(required) <= set(obj) or set(obj) - set(required) - set(optional):
        raise ValueError('Invalid packet fields')


def _string(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Invalid packet text')
    return value


def _id(value):
    value = _string(value)
    if len(value) > 120 or re.fullmatch(r'[A-Za-z][A-Za-z0-9:_-]*', value) is None:
        raise ValueError('Invalid packet ID')
    return value


def _ref(text, question):
    return {'whole_question': True} if text == question else {'literal': text}


def _unref(value, question):
    if isinstance(value, dict) and set(value) == {'whole_question'} and value['whole_question'] is True:
        return question
    _keys(value, ('literal',))
    return _string(value['literal'])


def compact_clause_context(payload):
    _keys(payload, ('question', 'request', 'parts'))
    question = _string(payload['question'])
    request = _string(payload['request'])
    if not isinstance(payload['parts'], list) or not payload['parts']:
        raise ValueError('Missing parts')
    parts, ids = [], set()
    for part in payload['parts']:
        _keys(part, ('id', 'request'))
        pid = _id(part['id'])
        if pid in ids:
            raise ValueError('Duplicate part ID')
        ids.add(pid)
        parts.append({'id': pid, 'request': _ref(_string(part['request']), question)})
    return {'contract': CLAUSE_CONTRACT, 'question': question, 'request': _ref(request, question), 'parts': parts}


def expand_clause_context(packet):
    _keys(packet, ('contract', 'question', 'request', 'parts'))
    if packet['contract'] != CLAUSE_CONTRACT:
        raise ValueError('Unknown clause contract')
    question = _string(packet['question'])
    if not isinstance(packet['parts'], list) or not packet['parts']:
        raise ValueError('Missing parts')
    parts = []
    for part in packet['parts']:
        _keys(part, ('id', 'request'))
        parts.append({'id': _id(part['id']), 'request': _unref(part['request'], question)})
    if len({p['id'] for p in parts}) != len(parts):
        raise ValueError('Duplicate part ID')
    return {'question': question, 'request': _unref(packet['request'], question), 'parts': parts}


def _validate_payload(payload):
    _keys(payload, ('question', 'request', 'parts', 'parent_context', 'facts', 'sources', 'support_visibility'), ('proposal', 'proposal_origin'))
    compact_clause_context({k: payload[k] for k in ('question', 'request', 'parts')})
    if payload['parent_context'] != PARENT_CONTEXT or payload['support_visibility'] != SUPPORT_VISIBILITY:
        raise ValueError('Unknown context contract')
    if ('proposal' in payload) != ('proposal_origin' in payload) or ('proposal' in payload and payload['proposal_origin'] != PROPOSAL_ORIGIN):
        raise ValueError('Unknown proposal contract')
    if not isinstance(payload['sources'], list) or not payload['sources'] or not isinstance(payload['facts'], list) or not payload['facts']:
        raise ValueError('Missing sources or facts')
    source_ids, part_ids, fact_ids = set(), {p['id'] for p in payload['parts']}, set()
    for source in payload['sources']:
        _keys(source, ('id', 'text'))
        sid = _id(source['id']); _string(source['text'])
        if sid in source_ids: raise ValueError('Duplicate source ID')
        source_ids.add(sid)
    forms = {'selected_text_availability_yes_no', 'selected_text_description', 'description_request'}
    for fact in payload['facts']:
        _keys(fact, ('id', 'part_id', 'request'), ('parent_request', 'answer_form', 'subject_reference', 'interpretation'))
        shape = set(fact)
        base = {'id', 'part_id', 'request'}
        if shape not in (base, base | {'answer_form'}, base | {'parent_request', 'answer_form', 'subject_reference', 'interpretation'}):
            raise ValueError('Invalid fact shape')
        fid = _id(fact['id']); _string(fact['request'])
        if fid in fact_ids or _id(fact['part_id']) not in part_ids: raise ValueError('Invalid fact binding')
        fact_ids.add(fid)
        if 'parent_request' in fact: _string(fact['parent_request'])
        if 'answer_form' in fact and fact['answer_form'] not in forms: raise ValueError('Invalid answer form')
        if 'subject_reference' in fact and fact['subject_reference'] not in ('unresolved_pronoun', 'explicit_document_subject'): raise ValueError('Invalid subject reference')
        if 'interpretation' in fact and fact['interpretation'] != INTERPRETATION: raise ValueError('Unknown interpretation')
        if shape == base | {'answer_form'} and fact['answer_form'] != 'selected_text_availability_yes_no':
            raise ValueError('Invalid availability fact')
        if 'interpretation' in fact and (fact['answer_form'], fact['subject_reference']) not in (
                ('selected_text_description', 'explicit_document_subject'), ('description_request', 'unresolved_pronoun')):
            raise ValueError('Invalid description fact')
    for part in payload['parts']:
        if ''.join(f['request'] for f in payload['facts'] if f['part_id'] == part['id']) != part['request']:
            raise ValueError('Fact literals do not reconstruct their part')
    if 'proposal' in payload:
        if not isinstance(payload['proposal'], dict) or set(payload['proposal']) != fact_ids: raise ValueError('Invalid proposal IDs')
        for row in payload['proposal'].values():
            _keys(row, ('decision', 'sources'))
            if row['decision'] not in ('yes', 'no', 'established', 'not_established', 'incompatible', 'mapping_unclear'): raise ValueError('Invalid proposal decision')
            ids = row['sources']
            if not isinstance(ids, list) or not 1 <= len(ids) <= 3 or any(not isinstance(x, str) or x not in source_ids for x in ids) or len(ids) != len(set(ids)): raise ValueError('Invalid proposal sources')


def compact(payload):
    _validate_payload(payload)
    base = compact_clause_context({k: payload[k] for k in ('question', 'request', 'parts')})
    parents, facts = [], []
    for fact in payload['facts']:
        row = {k: deepcopy(v) for k, v in fact.items() if k not in ('parent_request', 'subject_reference', 'interpretation')}
        parent = {k: fact[k] for k in ('parent_request', 'subject_reference') if k in fact}
        if parent:
            if parent not in parents: parents.append(parent)
            row['parent_id'] = 'PARENT' + str(parents.index(parent) + 1)
        if 'interpretation' in fact: row['interpretation_v1'] = True
        facts.append(row)
    packet = {'contract': CONTRACT, 'question': base['question'], 'request': base['request'], 'parts': base['parts'],
              'parents': [{'id': 'PARENT' + str(i + 1), **deepcopy(p)} for i, p in enumerate(parents)],
              'facts': facts, 'sources': deepcopy(payload['sources'])}
    if 'proposal' in payload: packet['proposal'] = deepcopy(payload['proposal'])
    return packet


def expand(packet):
    _keys(packet, ('contract', 'question', 'request', 'parts', 'parents', 'facts', 'sources'), ('proposal',))
    if packet['contract'] != CONTRACT: raise ValueError('Unknown packet contract')
    base = expand_clause_context({'contract': CLAUSE_CONTRACT, 'question': packet['question'], 'request': packet['request'], 'parts': packet['parts']})
    if not isinstance(packet['parents'], list) or not isinstance(packet['facts'], list): raise ValueError('Invalid parents or facts')
    parents = {}
    for index, parent in enumerate(packet['parents'], 1):
        _keys(parent, ('id',), ('parent_request', 'subject_reference'))
        if parent['id'] != 'PARENT' + str(index) or len(parent) == 1: raise ValueError('Invalid parent ID')
        value = {k: parent[k] for k in ('parent_request', 'subject_reference') if k in parent}
        if value in parents.values(): raise ValueError('Duplicate parent entry')
        parents[parent['id']] = value
    facts, used = [], set()
    for fact in packet['facts']:
        _keys(fact, ('id', 'part_id', 'request'), ('answer_form', 'parent_id', 'interpretation_v1'))
        row = {k: deepcopy(v) for k, v in fact.items() if k not in ('parent_id', 'interpretation_v1')}
        if 'parent_id' in fact:
            if _id(fact['parent_id']) not in parents: raise ValueError('Invalid parent reference')
            used.add(fact['parent_id']); row.update(deepcopy(parents[fact['parent_id']]))
        if 'interpretation_v1' in fact:
            if fact['interpretation_v1'] is not True: raise ValueError('Invalid interpretation reference')
            row['interpretation'] = INTERPRETATION
        facts.append(row)
    if used != set(parents): raise ValueError('Unused parent reference')
    payload = {**base, 'parent_context': PARENT_CONTEXT, 'facts': facts, 'sources': deepcopy(packet['sources']), 'support_visibility': SUPPORT_VISIBILITY}
    if 'proposal' in packet: payload.update(proposal=deepcopy(packet['proposal']), proposal_origin=PROPOSAL_ORIGIN)
    _validate_payload(payload)
    return payload
