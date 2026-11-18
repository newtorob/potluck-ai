"""Literal unresolved request spans with exact retained evidence; coverage is unverified."""
from __future__ import annotations

from copy import deepcopy
import json

from . import investigation_part_selection_v33 as prior
from . import reviewed_source_unit_plan as units

VERSION = 'fixed-part-selection-v34'
MODEL_INPUT_CONTRACT = VERSION
MAX_RAW_CHARS = prior.MAX_RAW_CHARS
MAX_UNRESOLVED = 6
SCOPE = prior.SCOPE
COVERAGE_INSTRUCTION = (
    'Assess EVERY requested fact inside each fixed part, including compound questions. '
    'For any fact not established by the supplied reviewed statement text, put its smallest complete '
    'literal question clause in unresolved, copied EXACTLY from that part.request. Include its property/subject; '
    'do not output isolated joiners or invented questions. List fragments in question order without overlap. '
    'Return an empty unresolved list ONLY when every requested fact is established; a fully supported explicit '
    'value or scoped negative must not become unknown. Missing observation/telemetry does not establish an actual total. '
    'A part with any unresolved fact must use unknown unless its selected evidence conflicts; preserve conflicting '
    'in that case and still list its unresolved facts. Retain the exact source basis for all known facts '
    'and all conditions, exceptions and safeguards. Keep this scope basis visible, not only background. '
    'Do not borrow another fact\'s nearby name, number, date or period. Do not claim whole-document/company absence. '
    'These are unverified model assessments, not exhaustive coverage proof. '
    'The JSON includes unresolved literal fragments in addition to source IDs/verdicts; no other answer prose.'
)
# Keep all established scope/condition rules while extending the output contract.
REVIEW_INSTRUCTION = prior.REVIEW_INSTRUCTION + ' ' + COVERAGE_INSTRUCTION


def prepare(question, evidence, part_plan, *, request=None, unit_plans=None):
    body = prior.prepare(question, evidence, part_plan, request=request, unit_plans=unit_plans)
    body.pop('input_sha256')
    body['version'] = VERSION
    return {**body, 'input_sha256': units.digest(body)}


def _base(prepared):
    if not isinstance(prepared, dict) or prepared.get('version') != VERSION:
        raise ValueError('Invalid unresolved-request input')
    body = {k: deepcopy(v) for k, v in prepared.items() if k != 'input_sha256'}
    if prepared.get('input_sha256') != units.digest(body):
        raise ValueError('Unresolved-request input binding mismatch')
    body['version'] = prior.VERSION
    base = {**body, 'input_sha256': units.digest(body)}
    prior.authored_inventory(base)  # Reconstruct and verify the complete legacy source/part binding.
    return base


def authored_inventory(prepared):
    result = prior.authored_inventory(_base(prepared))
    return {**result, 'version': VERSION, 'input_sha256': prepared['input_sha256']}


def _first(prepared, first):
    if first != authored_inventory(prepared):
        raise ValueError('Invalid unresolved-request inventory')


def _payload(prepared):
    result = prior._payload(_base(prepared))
    result['contract'] = MODEL_INPUT_CONTRACT
    return result


def review_messages(prepared, first):
    _first(prepared, first)
    return [{'role': 'system', 'content': REVIEW_INSTRUCTION},
            {'role': 'user', 'content': json.dumps(_payload(prepared), ensure_ascii=False)}]


def review_schema(prepared, first):
    _first(prepared, first)
    base = _base(prepared)
    schema = prior.review_schema(base, prior.authored_inventory(base))
    for item in schema['properties']['parts']['properties'].values():
        item['properties']['unresolved'] = {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 500}, 'maxItems': MAX_UNRESOLVED}
        item['required'].append('unresolved')
    return schema


def _spans(values, request):
    if not isinstance(values, list) or len(values) > MAX_UNRESOLVED:
        raise ValueError('Invalid unresolved request list')
    result = []
    for text in values:
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError('Unresolved fragment must bind uniquely to its question part')
        start = request.find(text)
        # A second overlapping occurrence is ambiguous too; str.count skips it.
        if start < 0 or request.find(text, start + 1) >= 0:
            raise ValueError('Unresolved fragment must bind uniquely to its question part')
        end = start + len(text)
        if result and start < result[-1]['end']:
            raise ValueError('Unresolved fragments overlap or are out of order')
        result.append({'text': text, 'start': start, 'end': end})
    return result


def decode_review(raw, prepared, first, *, _require_scope_anchor=True):
    _first(prepared, first)
    data = prior.prior._loads(raw, {'parts', 'safeguards'})
    base = _base(prepared)
    legacy = deepcopy(data)
    spans = {}
    rows = prepared['part_plan']['parts']
    if not isinstance(data['parts'], dict) or set(data['parts']) != {p['id'] for p in rows}:
        raise ValueError('Invalid unresolved request parts')
    for part in rows:
        item = data['parts'][part['id']]
        if not isinstance(item, dict) or set(item) != {'evidence', 'background', 'verdict', 'unresolved'}:
            raise ValueError('Invalid unresolved request envelope')
        spans[part['id']] = _spans(item['unresolved'], part['request'])
        if _require_scope_anchor and item['verdict'] == 'unknown' and not spans[part['id']]:
            raise ValueError('Unknown final answer must name an unresolved request fragment')
        legacy['parts'][part['id']].pop('unresolved')
        # A claimed complete answer cannot override its own unresolved facts.
        if spans[part['id']] and item['verdict'] == 'supported':
            legacy['parts'][part['id']]['verdict'] = 'unknown'
    if sum(map(len, spans.values())) > MAX_UNRESOLVED:
        raise ValueError('Too many unresolved request fragments')
    result = prior.decode_review(json.dumps(legacy, ensure_ascii=False), base,
                                 prior.authored_inventory(base), _require_scope_anchor=_require_scope_anchor)
    unresolved = []
    for row in result['parts']:
        row['unresolved_spans'] = deepcopy(spans[row['id']])
        row['unresolved_requests'] = [s['text'] for s in spans[row['id']]]
        unresolved.extend({'part_id': row['id'], **s} for s in spans[row['id']])
    for row in result['display_parts']:
        row['unresolved_spans'] = deepcopy(spans[row['id']])
        row['unresolved_requests'] = [s['text'] for s in spans[row['id']]]
    if unresolved:
        label = 'Not established by the selected reviewed statements (model assessment; review required):'
        result['text'] = label + '\n' + '\n'.join(f"- {s['part_id']}: {s['text']}" for s in unresolved) + '\n\n' + result['text']
    if len(result['text']) > units.MAX_VISIBLE:
        raise ValueError('Unresolved request presentation exceeds visible budget')
    result.update(version=VERSION, input_sha256=prepared['input_sha256'], review_raw=raw,
                  authored_inventory_sha256=units.digest(first), unresolved_requests=unresolved,
                  review_input_sha256=units.digest({'messages': review_messages(prepared, first), 'schema': review_schema(prepared, first)}))
    return result


def decode_proposal(raw, prepared, first):
    return decode_review(raw, prepared, first, _require_scope_anchor=False)


def review_focus(prepared, first, proposal_raw):
    proposal = decode_proposal(proposal_raw, prepared, first)
    base, legacy = _legacy_proposal(prepared, proposal_raw)
    return [*prior.review_focus(base, prior.authored_inventory(base), legacy),
            'all-requested-facts', *(['unresolved-question-fragments'] if proposal['unresolved_requests'] else [])]


def _legacy_proposal(prepared, raw):
    data = json.loads(raw)
    for item in data['parts'].values():
        if item.pop('unresolved') and item['verdict'] == 'supported':
            item['verdict'] = 'unknown'
    return _base(prepared), json.dumps(data, ensure_ascii=False)


def necessity_messages(prepared, first, proposal_raw):
    proposal = decode_proposal(proposal_raw, prepared, first)
    payload = _payload(prepared)
    payload['proposed_selection'] = json.loads(proposal_raw)
    payload['proposal_status'] = proposal['status']
    base, legacy = _legacy_proposal(prepared, proposal_raw)
    existing = prior.necessity_messages(base, prior.authored_inventory(base), legacy)[0]['content']
    return [{'role': 'system', 'content': existing + ' ' + COVERAGE_INSTRUCTION},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]


def validate_result(question, evidence, part_plan, review_raw, stored, *, request=None, unit_plans=None):
    prepared = prepare(question, evidence, part_plan, request=request, unit_plans=unit_plans)
    expected = decode_review(review_raw, prepared, authored_inventory(prepared))
    if expected != stored:
        raise ValueError('Unresolved-request result replay mismatch')
    return expected
