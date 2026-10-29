"""Owner-selected calculation inputs and evidence for a saved v9 draft."""
from copy import deepcopy
from typing import Any

from potluck_local.services import investigation_answers as answers
from potluck_local.services import investigation_calculations as calculations
from potluck_local.services.memory_safety import redact_credentials


def build(item: dict[str, Any], rows: Any) -> dict[str, Any]:
    provenance = item.get('provenance') or {}
    if item.get('status') != 'draft' or provenance.get('template_version') != 'investigation-v9':
        raise ValueError('Review requires a v9 draft')
    snippets = answers.snippets_from_evidence(item['evidence'])
    if snippets != provenance.get('snippet_catalog'):
        raise ValueError('Invalid source snippets')
    context, lengths = {}, {}
    for record in item['evidence']:
        for child in record['sources'] if record['kind'] == 'reviewed_claim' else [record]:
            cid = child['citation_id']
            length = len(child.get('presented_content', ''))
            if length > lengths.get(cid, 0):
                context[cid], lengths[cid] = child['content'], length
    version = provenance.get('quantity_parser_version', 'v2')
    if version not in ('v2', 'v3'):
        raise ValueError('Invalid quantity parser version')
    quantities = calculations.quantity_catalog(snippets, source_context=context, version=version)
    if quantities != provenance.get('quantity_catalog'):
        raise ValueError('Invalid source quantities')
    plans = {p['id']: p for p in provenance['operation_plan'] if p['operation'] != 'answer'}
    by_snippet = {s['snippet_id']: s for s in snippets if s['quote'].strip()}
    by_quantity = {q['id']: q for q in quantities}
    if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
        raise ValueError('Invalid calculation reviews')
    result, audits, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'request_id', 'status', 'operand_ids', 'snippet_ids', 'reason'}:
            raise ValueError('Invalid calculation review')
        rid, status, ids, sids, reason = (row[k] for k in ('request_id', 'status', 'operand_ids', 'snippet_ids', 'reason'))
        if (not isinstance(rid, str) or rid not in plans or rid in seen
                or status not in ('calculate', 'missing', 'unsupported', 'conflicting')
                or not isinstance(ids, list) or len(ids) > 2 or any(not isinstance(x, str) or x not in by_quantity for x in ids)
                or len(set(ids)) != len(ids) or not isinstance(sids, list) or len(sids) > 8
                or any(not isinstance(x, str) or x not in by_snippet for x in sids) or len(set(sids)) != len(sids)
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000):
            raise ValueError('Invalid calculation review')
        seen.add(rid)
        sids = list(sids)
        for qid in ids:
            sid = by_quantity[qid]['snippet_id']
            if sid not in sids:
                sids.append(sid)
        if len(sids) > 8 or (by_snippet and not sids):
            raise ValueError('Source evidence required')
        operation = plans[rid]['operation']
        reason = redact_credentials(reason.strip())
        if status == 'calculate':
            if len(ids) != 2:
                raise ValueError('Two inputs required')
            trace = calculations.calculate(operation, ids, quantities, version=version)
            if trace['status'] != 'computed' and trace['reason'] not in ('The baseline is zero.', 'The total is zero.'):
                raise ValueError('Incompatible calculation inputs')
            if trace['status'] == 'unknown':
                trace['limitation_kind'] = 'zero_denominator'
            trace['assessment'] = 'deterministic calculator'
            trace['source_selection_assessment'] = 'Reviewer-selected inputs and sources; authority is not verified.'
        else:
            trace = {'status': 'conflicting' if status == 'conflicting' else 'unknown',
                     'text': 'Reviewer assessment (' + status + '): ' + reason,
                     'reason': reason, 'inputs': [deepcopy(by_quantity[x]) for x in ids],
                     'result': None, 'unit': None, 'formula': None, 'rounding': None,
                     'limitation_kind': status, 'assessment': 'reviewer assessment',
                     'caveat': 'This assessment does not verify source completeness or authority.'}
        trace = {'request_id': rid, 'operation': operation, **trace}
        answer = {'question_id': rid, 'status': 'supported' if trace['status'] == 'computed' else trace['status'],
                  'text': trace['text'], 'snippet_ids': sids,
                  'source_excerpts': [deepcopy(by_snippet[s]) for s in sids], 'calculation': trace}
        result.append(answer)
        audits.append({'request_id': rid, 'status': status, 'operand_ids': list(ids),
                       'snippet_ids': sids, 'reason': reason, 'trace': deepcopy(trace)})
    return {'answers': result, 'calculation_reviews': audits}
