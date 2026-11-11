"""Per-part source evidence roles; structural checks do not prove correctness."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from . import investigation_part_selection_v28 as model_wire

from potluck_local.services import investigation_part_selection as prior
from potluck_local.services import reviewed_source_unit_plan as source_units
from .investigation_part_basis_presentation import render as render_shared

VERSION = 'fixed-part-selection-v32'
MAX_RAW_CHARS = prior.MAX_RAW_CHARS
SCOPE = ('Selected reviewed statements only. Exact evidence and declared dependencies/safeguards are displayed; optional background is retained separately. Relevance, required-context coverage and background classification need human review.')
ROLES = ('evidence_unit_ids', 'context_unit_ids')
MODEL_INPUT_CONTRACT = model_wire.VERSION
REVIEW_INSTRUCTION = model_wire.REVIEW_INSTRUCTION


def prepare(question, evidence, part_plan, *, request=None, unit_plans=None):
    base = prior.prepare(question, evidence, part_plan, request=request, unit_plans=unit_plans)
    body = {k: v for k, v in base.items() if k != 'input_sha256'}
    body['version'] = VERSION
    return {**body, 'input_sha256': source_units.digest(body)}


def _base(prepared):
    if not isinstance(prepared, dict):
        raise ValueError('Invalid per-part role input')
    try:
        evidence = [{'kind': 'reviewed_claim', 'citation_id': c['citation_id'],
                     'content': c['text'], 'sources': c['sources']} for c in prepared['claims']]
        plans = {c['citation_id']: prepared['catalog']['plans'][c['id']]
                 for c in prepared['claims'] if c['id'] in prepared['catalog']['plans']}
        expected = prepare(prepared['question'], evidence, prepared['part_plan'],
                           request=prepared['request'], unit_plans=plans)
        if prepared != expected:
            raise ValueError('Per-part role input binding mismatch')
        return prior.prepare(prepared['question'], evidence, prepared['part_plan'],
                             request=prepared['request'], unit_plans=plans)
    except (KeyError, TypeError) as exc:
        raise ValueError('Invalid per-part role input') from exc


def _payload(prepared):
    payload = prior._payload(_base(prepared))
    payload['contract'] = MODEL_INPUT_CONTRACT
    payload['support_visibility'] = 'Reviewed statement text is provided in claims[].units[].text; full supporting original documents are not provided. Absence is selected-statement scoped.'
    return payload


def authored_inventory(prepared):
    """Application state only; no native response or relevance assertion."""
    _base(prepared)
    return {'version': VERSION, 'input_sha256': prepared['input_sha256'],
            'origin': 'application-authored-empty-not-model-reply',
            'inventory': {unit['id']: [] for unit in prepared['catalog']['units']}}


def _first(prepared, selection):
    if selection != authored_inventory(prepared):
        raise ValueError('Invalid application-authored inventory')


def review_messages(prepared, selection):
    _first(prepared, selection)
    payload = _payload(prepared)
    return [{'role': 'system', 'content': REVIEW_INSTRUCTION},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]


def review_schema(prepared, selection):
    _first(prepared, selection)
    ids = [u['id'] for u in prepared['catalog']['units']]
    keys = [p['id'] for p in prepared['part_plan']['parts']]
    ids_array = {'type':'array','items':{'type':'string','enum':ids},'maxItems':source_units.MAX_CLOSED}
    item = {'type':'object','properties':{
        'evidence':deepcopy(ids_array),'background':deepcopy(ids_array),
        'verdict':{'type':'string','enum':['supported','unknown','conflicting']}},
        'required':['evidence','background','verdict'],'additionalProperties':False}
    part_map={'type':'object','properties':{key:deepcopy(item) for key in keys},
              'required':keys,'additionalProperties':False}
    return {'type':'object','properties':{'parts':part_map,'safeguards':deepcopy(ids_array)},
            'required':['parts','safeguards'],'additionalProperties':False}



def decode_review(raw, prepared, selection, *, _require_scope_anchor=True):
    if type(_require_scope_anchor)is not bool:raise ValueError('Invalid scope-anchor gate')
    _first(prepared, selection)
    parts = prepared['part_plan']['parts']
    data = prior._loads(raw, {'parts', 'safeguards'})
    global_ids = prior._ids(prepared, data['safeguards'])
    part_data = data['parts']
    keys = [p['id'] for p in parts]
    available = [u['id'] for u in prepared['catalog']['units']]
    if not isinstance(part_data,dict) or set(part_data)!=set(keys):
        raise ValueError('Invalid review parts')
    rows = []
    claims = {c['id']: c for c in prepared['claims']}
    for part in parts:
        item = part_data[part['id']]
        if not isinstance(item,dict) or set(item)!={'verdict','evidence','background'}:
            raise ValueError('Invalid review group envelope')
        evidence_ids=prior._ids(prepared,item['evidence'])
        context_ids=prior._ids(prepared,item['background'])
        verdict=item['verdict']
        if verdict not in ('supported','unknown','conflicting') or not isinstance(verdict,str):
            raise ValueError('Invalid explicit relation')
        if verdict!='unknown' and not evidence_ids:
            raise ValueError('Relation requires appropriate evidence')
        if len(set(evidence_ids+context_ids))!=len(evidence_ids+context_ids):
            raise ValueError('Overlapping per-part groups')
        requested = [uid for uid in available if uid in evidence_ids or uid in context_ids]
        roles = {'evidence_unit_ids':[uid for uid in available if uid in evidence_ids],
                 'context_unit_ids':[uid for uid in available if uid in context_ids]}
        rendered = source_units.render(prepared['catalog'], requested)
        closed = rendered['closed_unit_ids']
        passages = [{'unit_id': u['id'], 'citation_id': claims[u['claim_id']]['citation_id'],
                     'ranges': deepcopy(u['ranges']), 'text': u['text']}
                    for u in prepared['catalog']['units'] if u['id'] in closed]
        rows.append({**part, **roles, 'background_unit_ids': deepcopy(roles['context_unit_ids']), 'verdict': verdict, 'requested_unit_ids': requested,
                     'unit_ids': closed, 'dependency_only_unit_ids': [u for u in closed if u not in requested],
                     'source_text': rendered['text'], 'passages': passages})
    if (_require_scope_anchor and all(row['verdict']=='unknown' for row in rows)
            and not global_ids and not any(row['evidence_unit_ids'] for row in rows)):
        raise ValueError('Wholly unknown answer needs a relevant selected-source scope anchor')
    part_ids = prior._aggregate(rows)
    rendered = source_units.render(prepared['catalog'], list(dict.fromkeys(part_ids + global_ids)))
    context_closed = source_units.render(prepared['catalog'], global_ids)['closed_unit_ids']
    # The main answer uses evidence closure, never optional background alone.
    display_rows=[]
    for row in rows:
        visible=source_units.render(prepared['catalog'],row['evidence_unit_ids'])
        ids=visible['closed_unit_ids'];evidence=row['evidence_unit_ids']
        passages=[deepcopy(p) for p in row['passages'] if p['unit_id'] in ids]
        view={**deepcopy(row),'context_unit_ids':[],
              'requested_unit_ids':deepcopy(evidence),'unit_ids':ids,
              'dependency_only_unit_ids':[uid for uid in ids if uid not in evidence],
              'source_text':visible['text'],'passages':passages}
        display_rows.append(view)
        row['visible_unit_ids']=deepcopy(ids)
        row['visible_dependency_unit_ids']=deepcopy(view['dependency_only_unit_ids'])
        row['part_background_only_unit_ids']=[uid for uid in row['unit_ids'] if uid not in ids]
    already_visible={uid for row in display_rows for uid in row['unit_ids']}
    context_passages = [{'unit_id': u['id'], 'citation_id': claims[u['claim_id']]['citation_id'],
                         'ranges': deepcopy(u['ranges']), 'text': u['text']}
                        for u in prepared['catalog']['units'] if u['id'] in context_closed]
    visible_context = [p for p in context_passages if p['unit_id'] not in already_visible]
    visible_all=already_visible|set(context_closed)
    visible_ids=[uid for uid in rendered['closed_unit_ids'] if uid in visible_all]
    background_only=[uid for uid in rendered['closed_unit_ids'] if uid not in visible_all]
    text = render_shared(display_rows, {'unit_ids':context_closed,'passages':context_passages}, max_visible=source_units.MAX_VISIBLE)['text']
    if len(text) > source_units.MAX_VISIBLE:
        raise ValueError('Per-part visible answer limit')
    status = ('conflicting' if any(p['verdict'] == 'conflicting' for p in rows) else
              'unknown' if any(p['verdict'] == 'unknown' for p in rows) else 'supported')
    return {'version': VERSION, 'input_sha256': prepared['input_sha256'],
            'plan_sha256': prepared['part_plan']['plan_sha256'],
            'question': prepared['question'], 'request': prepared['request'],
            'plan_origin': prepared['part_plan']['origin'], 'coverage': 'not_verified',
            'review_raw': raw, 'scope_anchor_gate_applied': _require_scope_anchor,
            'authored_inventory_sha256': source_units.digest(selection),
            'review_input_sha256': source_units.digest({
                'messages': review_messages(prepared, selection),
                'schema': review_schema(prepared, selection)}),
            'parts': rows, 'request_context': {'requested_unit_ids':global_ids, 'unit_ids':context_closed,
                    'visible_unit_ids':[p['unit_id'] for p in visible_context], 'passages':context_passages},
            'status': status, 'unit_ids': rendered['closed_unit_ids'],
            'source_text': rendered['text'], 'text': text, 'display_parts': deepcopy(display_rows), 'visible_unit_ids':visible_ids,'background_only_unit_ids':background_only,'background_role': 'optional; context_unit_ids is a compatibility alias', 'scope': SCOPE, 'needs_review': True}


def validate_result(question, evidence, part_plan, review_raw, stored, *,
                    request=None, unit_plans=None):
    prepared = prepare(question, evidence, part_plan, request=request, unit_plans=unit_plans)
    expected = decode_review(review_raw, prepared, authored_inventory(prepared))
    if expected != stored:
        raise ValueError('Per-part role result replay mismatch')
    return expected


def decode_proposal(raw,prepared,selection):
    return decode_review(raw,prepared,selection,_require_scope_anchor=False)



# Focuses alter instructions only; every source unit and fixed part stays available.
PRECISION_INSTRUCTION = (
 'Audit each requested precise property against the source subject, event, units and trigger. A deadline for a review, record or notification does not establish a deadline for finishing a different action. '
 'Change the absent requested property to unknown rather than borrow a nearby number or date. Keep all other known requested facts in their own supported parts. '
)
UNKNOWN_INSTRUCTION = (
 'A wholly unknown final answer still needs a relevant visible selected-source scope basis: preserve the referenced rule/event and its known relative trigger or period even when the requested precise properties are absent. '
 'Do not put that basis only in optional background, prune it away, cite unrelated material, or imply whole-document absence. '
)
REFERENCE_INSTRUCTION = (
 'Inspect explicit references in the proposed visible source basis. An explicit reference to another passage, section or TODO is not complete by itself when the referenced approved clause supplies a required risk, limitation or prerequisite defining the requested answer. '
 'Keep that necessary qualification visible, not merely optional background. Original-document location words do not mean catalog order. Do not add unrelated implementation or optional metadata. '
)
_PRECISION = re.compile(
 r'\b(?:exact|precise|precisely|time\s*zone|timestamp|expiry|expiration|expires|how\s+many|how\s+long|number\s+of|duration|threshold)\b'
 r'|\b(?:what|which)\s+(?:(?:is|are)\s+(?:the\s+)?)?(?:specific\s+)?(?:date|time|deadline|interval|frequency)\b', re.I)
_REFERENCE = re.compile(r'\bsee\b|\brefer(?:s|red|ring)?\s+to\b|\bas\s+(?:noted|described|stated|specified)\s+(?:in|above|below)\b', re.I)


def review_focus(prepared, selection, proposal_raw):
 proposal = decode_proposal(proposal_raw, prepared, selection)
 requested = ' '.join([prepared['question'], prepared['request']] + [p['request'] for p in prepared['part_plan']['parts']])
 labels=[]
 if _PRECISION.search(requested):labels.append('precise-property-and-event')
 if all(row['verdict']=='unknown' for row in proposal['parts']):labels.append('unknown-visible-scope')
 visible=set(proposal['visible_unit_ids'])
 if any(u['id'] in visible and _REFERENCE.search(u['text']) for u in prepared['catalog']['units']):labels.append('explicit-source-reference')
 return labels


def necessity_messages(prepared, selection, proposal_raw):
 proposal = decode_proposal(proposal_raw, prepared, selection)
 payload = _payload(prepared)
 payload['proposed_selection'] = json.loads(proposal_raw)
 payload['proposal_status'] = proposal['status']
 instructions={'precise-property-and-event':PRECISION_INSTRUCTION,
               'unknown-visible-scope':UNKNOWN_INSTRUCTION,
               'explicit-source-reference':REFERENCE_INSTRUCTION}
 focuses=review_focus(prepared,selection,proposal_raw)
 system = model_wire.NECESSITY_INSTRUCTION + ' ' + REVIEW_INSTRUCTION
 if focuses:system += ' ' + ' '.join(instructions[label] for label in focuses)
 return [{'role':'system','content':system},
         {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
