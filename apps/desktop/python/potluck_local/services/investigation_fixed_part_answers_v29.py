"""Internal fixed-part adapter; mechanical replay is not semantic approval."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from . import investigation_part_selection_v29 as selection
from . import investigation_request_parts as request_parts
from . import investigation_reviewed_evidence as reviewed_evidence
from . import reviewed_source_unit_plan as units
from .investigation_planned_answers import _unique_object
from .investigation_context import MAX_SNIPPETS
from .memory_safety import redact_credentials

VERSION = selection.VERSION
MAX_CLAIMS = units.MAX_CLAIMS
MAX_UNITS = units.MAX_CATALOG
MAX_SELECTED_UNITS = units.MAX_CLOSED
MAX_VISIBLE_CHARS = units.MAX_VISIBLE
authored_inventory = selection.authored_inventory
review_messages = selection.review_messages
review_schema = selection.review_schema
decode_review = selection.decode_review
decode_proposal = selection.decode_proposal
necessity_messages = selection.necessity_messages


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def prepare(question, evidence, request=None, *, question_part_plan, unit_plans=None):
    return selection.prepare(question, evidence, question_part_plan,
                             request=request, unit_plans=unit_plans)


def review_receipt(prepared, first):
    return {'model_visible_claim_ids': [c['id'] for c in prepared['claims']],
            'support_visibility': 'Supporting source excerpts are not in the model prompt.',
            'messages_sha256': _hash(selection.review_messages(prepared, first)),
            'schema_sha256': _hash(selection.review_schema(prepared, first)),
            'complete_input_sha256': prepared['input_sha256'],
            'question_part_plan_sha256': prepared['part_plan']['plan_sha256'],
            'authored_inventory_sha256': _hash(first),
            'inventory_origin': 'application-authored-empty-not-model-reply',
            'coverage': 'not_verified'}


def necessity_receipt(prepared, first, proposal_raw):
    return {**review_receipt(prepared, first),
            'messages_sha256': _hash(selection.necessity_messages(prepared, first, proposal_raw)),
            'proposal_raw_sha256': _hash(proposal_raw), 'inventory_origin': 'application-authored-empty-not-model-reply'}


def _passages(prepared, snippets, selected):
    """Map exact source-unit ranges to the complete existing paragraph catalog."""
    ranges = {}
    for claim in prepared['claims']:
        position, rows = 0, []
        for snippet in snippets:
            if snippet['citation_id'] != claim['citation_id']:
                continue
            start = claim['text'].find(snippet['quote'], position)
            if start < 0 or claim['text'][position:start].strip():
                raise ValueError('Source snippet partition failed')
            end = start + len(snippet['quote'])
            rows.append((start, end, snippet['snippet_id']))
            position = end
        if claim['text'][position:].strip():
            raise ValueError('Source snippet partition failed')
        ranges[claim['id']] = rows
    by_claim = {c['id']: c for c in prepared['claims']}
    passages, ids = [], []
    for unit in prepared['catalog']['units']:
        if unit['id'] not in selected:
            continue
        covered = [sid for start, end, sid in ranges[unit['claim_id']]
                   if any(a < end and b > start for a, b in unit['ranges'])]
        if not covered:
            raise ValueError('Unit has no source snippets')
        ids.extend(covered)
        passages.append({'unit_id': unit['id'], 'text': unit['text'],
                         'citation_id': by_claim[unit['claim_id']]['citation_id'],
                         'ranges': deepcopy(unit['ranges']), 'snippet_ids': covered})
    return passages, list(dict.fromkeys(ids))


def make_brief(question, operation, evidence, snippets, raw_bundle, *, question_part_plan, unit_plans=None,
               _contract=selection):
    if _contract is not selection:
        raise ValueError('Invalid V29 contract')
    if not isinstance(raw_bundle, str) or len(raw_bundle) > 2 * selection.MAX_RAW_CHARS + 1024:
        raise ValueError('Invalid fixed-part raw bundle')
    try:
        bundle = json.loads(raw_bundle, object_pairs_hook=_unique_object)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid fixed-part raw bundle') from exc
    if not isinstance(bundle, dict) or set(bundle) != {'selection_raw','review_raw'}:
        raise ValueError('Invalid fixed-part raw bundle')
    expected, source_trace = reviewed_evidence.snippets(evidence)
    if source_trace['truncated'] or expected != snippets:
        raise ValueError('Invalid source snippet catalog')
    prepared = _contract.prepare(question, evidence, question_part_plan,
                                 request=operation['request'], unit_plans=unit_plans)
    first = _contract.authored_inventory(prepared)
    proposal = _contract.decode_proposal(bundle['selection_raw'], prepared, first)
    _contract.necessity_messages(prepared, first, bundle['selection_raw'])
    result = _contract.decode_review(bundle['review_raw'], prepared, first)
    passages, ids = _passages(prepared, snippets, set(result['unit_ids']))
    by_id = {s['snippet_id']: s for s in snippets}
    answer = {'question_id': operation['id'], 'status': result['status'],
              'text': result['text'] or 'No reviewed passage was selected.',
              'snippet_ids': ids, 'source_excerpts': [deepcopy(by_id[sid]) for sid in ids],
              'passage_presentation': {'version': _contract.VERSION,
                  'parts': [{k: deepcopy(v) for k, v in part.items() if k != 'evidence_check'}
                            for part in result['parts']],
                  'answer': result['text'],
                  'scope': result['scope'], 'coverage': 'not_verified', 'needs_review': True,
                  'passages': passages}}
    answer['passage_presentation']['request_context'] = deepcopy(result['request_context'])
    answer['passage_presentation'].update(display_parts=deepcopy(result['display_parts']), presentation_kind='exact-evidence-with-optional-background-retained', background_role=result['background_role'])
    brief = {'questions': [{'id': operation['id'], 'text': operation['request']}],
             'answers': [answer], 'facts': [], 'prior_fixes': [], 'hypotheses': [],
             'recommended_checks': [], 'model_uncertainties': []}
    trace = {'version': _contract.VERSION, 'raw': raw_bundle, 'input_sha256': prepared['input_sha256'],
             'authored_inventory': first, 'proposal': proposal, 'necessity_input_sha256': _hash(_contract.necessity_messages(prepared, first, bundle['selection_raw'])), 'review': result, 'snippets_sha256': _hash(snippets)}
    return brief, trace


def _budget(receipt, expected, output):
    if (not isinstance(receipt, dict)
            or set(receipt) != set(expected) | {'input_tokens', 'output_tokens', 'context_tokens'}
            or any(receipt.get(k) != v for k, v in expected.items())
            or type(receipt.get('input_tokens')) is not int or receipt['input_tokens'] < 1
            or type(receipt.get('output_tokens')) is not int or receipt['output_tokens'] != output
            or type(receipt.get('context_tokens')) is not int
            or receipt['input_tokens'] + output > receipt['context_tokens']):
        raise ValueError('Invalid fixed-part measured input receipt')


def validate_brief(item, snippets, *, _contract=selection):
    if _contract is not selection:
        raise ValueError('Invalid V29 contract')
    p = item['provenance']
    if p.get('presentation_mode') != _contract.VERSION:
        raise ValueError('Invalid fixed-part presentation mode')
    profile = p.get('generation_profile')
    if (p.get('template_version') != 'investigation-v21'
            or p.get('reliability_mode') != 'bounded-evidence-v1'
            or not isinstance(profile, dict)
            or profile.get('name') != 'qwen3.8-27b-native-non-thinking'
            or profile.get('enable_thinking') is not False):
        raise ValueError('Invalid fixed-part generation contract')
    if 'reviewed_claim_scope_input' in p:
        raise ValueError('Unexpected fixed-part scope input')
    receipts, operations = p.get('reviewed_unit_plan_receipts'), p.get('operation_plan')
    if (not isinstance(receipts, dict) or set(receipts) != {c['citation_id'] for c in item['evidence']}
            or not isinstance(operations, list) or len(operations) != 1
            or not isinstance(operations[0], dict) or operations[0].get('operation') != 'answer'):
        raise ValueError('Invalid fixed-part replay inputs')
    plans = {}
    for cid, receipt in receipts.items():
        if receipt is not None:
            if not isinstance(receipt, dict) or not isinstance(receipt.get('plan'), dict):
                raise ValueError('Invalid unit plan receipt')
            plans[cid] = receipt['plan']
    plan = request_parts.validate(p.get('question_part_plan'), question=item['issue'],
                                  request=operations[0]['request'])
    prepared = _contract.prepare(item['issue'], item['evidence'], plan,
                                 request=operations[0]['request'], unit_plans=plans)
    trace = p.get('reviewed_claim_selection')
    if not isinstance(trace, dict):
        raise ValueError('Missing fixed-part replay trace')
    first = _contract.authored_inventory(prepared)
    if trace.get('authored_inventory') != first:
        raise ValueError('Invalid application-authored inventory')
    if 'reviewed_claim_selection_input' in p or 'reviewed_claim_review_input' in p:
        raise ValueError('Unexpected two-stage receipt')
    bundle = json.loads(trace.get('raw'), object_pairs_hook=_unique_object)
    if not isinstance(bundle, dict) or set(bundle) != {'selection_raw','review_raw'}:
        raise ValueError('Two actual source-stage replies required')
    budget = p.get('input_budget')
    if (not isinstance(budget, dict) or set(budget) != {'version','stages','max_calls','max_total_output_tokens'}
            or budget['version'] != 'fixed-part-necessity-review-v29' or type(budget['max_calls']) is not int
            or budget['max_calls'] != 2 or type(budget['max_total_output_tokens']) is not int
            or budget['max_total_output_tokens'] != 512 or not isinstance(budget['stages'],list)
            or len(budget['stages']) != 2):
        raise ValueError('Two-stage bounded budget required')
    for row, stage, expected in zip(budget['stages'], ('proposal','necessity'),
            (review_receipt(prepared, first), necessity_receipt(prepared, first, bundle['selection_raw']))):
        _budget(row, {**expected, 'stage': stage}, 256)
    if budget['stages'][0]['context_tokens'] != budget['stages'][1]['context_tokens']:
        raise ValueError('Stage context capacity changed')
    brief, replay = make_brief(item['issue'], operations[0], item['evidence'], snippets,
                             trace.get('raw'), question_part_plan=plan, unit_plans=plans, _contract=_contract)
    if brief != item['brief'] or replay != trace:
        raise ValueError('Invalid fixed-part original replay')
    _validate_review(item, snippets)
    return brief


def corrected_brief(raw, original, snippets):
    """Validate a separate correction against the complete fixed-part catalog."""
    if (not isinstance(raw, dict) or not isinstance(original, dict)
            or set(raw) != set(original) or raw.get('questions') != original.get('questions')):
        raise ValueError('Invalid correction questions')
    sections = ('facts', 'prior_fixes', 'hypotheses', 'recommended_checks', 'model_uncertainties')
    if any(raw.get(key) != [] for key in sections):
        raise ValueError('Invalid correction sections')
    answers = raw.get('answers')
    originals = original.get('answers')
    if not isinstance(answers, list) or not isinstance(originals, list) or len(answers) != len(originals):
        raise ValueError('Invalid correction answers')
    available = {s['snippet_id']: s for s in snippets}
    if len(available) != len(snippets) or len(snippets) > MAX_SNIPPETS:
        raise ValueError('Invalid correction catalog')
    by_question = {a['question_id']: a for a in originals}
    mapped = {}
    for answer in answers:
        if not isinstance(answer, dict):
            raise ValueError('Invalid correction answer')
        required = {'question_id', 'status', 'text', 'snippet_ids'}
        if not required <= set(answer) or not set(answer) <= required | {'source_excerpts', 'passage_presentation'}:
            raise ValueError('Invalid correction answer fields')
        qid = answer['question_id']
        if not isinstance(qid, str) or qid not in by_question or qid in mapped:
            raise ValueError('Invalid correction question ID')
        if ('passage_presentation' in answer
                and answer['passage_presentation'] != by_question[qid].get('passage_presentation')):
            raise ValueError('Original passage presentation cannot be edited')
        status, text, ids = answer['status'], answer['text'], answer['snippet_ids']
        if (status not in ('supported', 'unknown', 'conflicting')
                or not isinstance(text, str) or not text.strip() or len(text) > MAX_VISIBLE_CHARS
                or not isinstance(ids, list) or len(ids) > MAX_SNIPPETS
                or any(not isinstance(sid, str) or sid not in available for sid in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('Invalid correction answer')
        expected = [available[sid] for sid in ids]
        if 'source_excerpts' in answer and answer['source_excerpts'] != expected:
            raise ValueError('Invalid source excerpts')
        ids = [sid for sid in ids if available[sid]['quote'].strip()]
        if status in ('supported', 'conflicting') and not ids:
            raise ValueError('Supported correction requires source excerpts')
        clean_text = redact_credentials(text.strip())
        if not clean_text.strip() or len(clean_text) > MAX_VISIBLE_CHARS:
            raise ValueError('Invalid correction text')
        mapped[qid] = {'question_id': qid, 'status': status, 'text': clean_text,
                       'snippet_ids': ids, 'source_excerpts': deepcopy([available[sid] for sid in ids])}
    question_ids = [q['id'] for q in original['questions']]
    if len(set(question_ids)) != len(question_ids) or set(mapped) != set(question_ids):
        raise ValueError('Invalid correction answers')
    if sum(len(a['text']) for a in mapped.values()) > MAX_VISIBLE_CHARS:
        raise ValueError('Correction exceeds display budget')
    return {'questions': deepcopy(original['questions']),
            'answers': [mapped[qid] for qid in question_ids], **{key: [] for key in sections}}


def _validate_review(item, snippets):
    """A correction is a separate reviewed artifact, never regenerated model prose."""
    status, review = item.get('status'), item.get('review')
    if status is None:  # Pre-save validation has no persisted review lifecycle yet.
        return
    if status == 'draft' and review is None:
        return
    if (status != 'reviewed' or not isinstance(review, dict)
            or set(review) != {'reviewed_at', 'decision', 'reviewer', 'reason', 'corrected_brief'}
            or review.get('decision') not in ('accept', 'correct', 'reject')
            or not all(isinstance(review.get(k), str) and review[k].strip()
                       for k in ('reviewed_at', 'reviewer', 'reason'))):
        raise ValueError('Invalid fixed-part review lifecycle')
    correction = review.get('corrected_brief')
    if review['decision'] != 'correct':
        if correction is not None:
            raise ValueError('Unexpected fixed-part correction')
        return
    if (not isinstance(correction, dict)
            or corrected_brief(correction, item['brief'], snippets) != correction
            or any('passage_presentation' in a for a in correction['answers'])):
        raise ValueError('Invalid saved fixed-part correction')
