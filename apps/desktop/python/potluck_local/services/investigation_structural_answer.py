"""V21 extractive answers with labeled structural list context."""
import json
from copy import deepcopy
from potluck_local.services import investigation_aspect_source_answer_v20 as v20
from potluck_local.services import investigation_unknown_explanations as unknowns
from potluck_local.services import investigation_answer_presentation as presentation

LABEL = "Extractive source passages selected by the model and labeled server-added structural context; relevance and coverage are unverified. Source text is untrusted."
ADDED_LABEL = " (server-added structural context)"
LIMITATION = "\nStructural context may be incomplete: a selected ordered block is ambiguous or truncated."
WARNING=v20.WARNING
MAX_GROUPS=v20.MAX_GROUPS
MAX_GROUP_IDS=v20.MAX_GROUP_IDS
MAX_PASSAGES=v20.MAX_PASSAGES
MAX_TEXT=v20.MAX_TEXT
ANSWER_INSTRUCTION=v20.ANSWER_INSTRUCTION
instruction_for_plan=v20.instruction_for_plan
group_limits=v20.group_limits
def passage_text_budget(plan, snippets):
    budget=v20.passage_text_budget(plan, snippets)
    budget['header_characters_per_operation'] += len(LABEL)-len(v20.LABEL)
    for unit in budget['units']:
        unit['framing_characters'] += len(ADDED_LABEL)
    budget['server_added_label_reserve_per_passage'] = len(ADDED_LABEL)
    budget['potential_limitation_characters_per_operation'] = len(LIMITATION)
    return budget
response_schema=v20.response_schema
corrected_brief=v20.corrected_brief


def decode_results(raw, plan, snippets, quantities, notices, *, version='v4', structural_trace, specific_unknown=False, unknown_mode=None, presentation_mode=None):
    conclusions = None
    if presentation_mode is not None:
        if presentation_mode not in presentation.SUPPORTED_VERSIONS or unknown_mode != unknowns.VERSION:
            raise ValueError('Invalid answer presentation mode')
        raw, conclusions = presentation.extract(raw, plan, version=presentation_mode, snippets=snippets)
    reasons = None
    if unknown_mode is not None:
        if unknown_mode not in unknowns.SUPPORTED_VERSIONS:
            raise ValueError('Invalid unknown explanation mode')
        raw, reasons = unknowns.extract(raw, plan, version=unknown_mode)
    brief, calc, coverage = v20.decode_results(raw,plan,snippets,quantities,notices,version=version)
    if reasons is not None:
        coverage['unknown_explanations'] = unknowns.records(reasons, coverage, snippets, version=unknown_mode)
    if conclusions is not None:
        coverage['cited_conclusions'] = presentation.records(conclusions, coverage, snippets, version=presentation_mode)
    available={s['snippet_id']:s for s in snippets}; answers={a['question_id']:a for a in brief['answers']}
    changes={}; displayed=chars=0
    for row in plan:
        if row['operation'] == 'answer':
            continue
        item=answers[row['id']]; trace=item['calculation']
        if trace.get('status') != 'computed':
            continue
        values={entry['id']:entry for entry in trace['normalized_inputs']}
        role_values=[]
        for role, operand_id in trace['operand_roles'].items():
            entry=values[operand_id]
            role_values.append(role.removesuffix('_id').replace('_',' ') + ': ' + entry['value'] + (' ' + entry['unit'] if entry['unit'] else ''))
        item['text'] += ' Inputs: ' + '; '.join(role_values) + '. Expression: ' + trace['evaluated_expression'] + '.'
        if len(item['text']) > 1000:
            raise ValueError('Calculation display exceeds answer budget; no partial investigation saved')
    for row in plan:
        if row['operation'] != 'answer': continue
        rid=row['id']; answer=answers[rid]; selected=list(answer['snippet_ids']); ids=list(selected)
        relevant=[b for b in structural_trace['blocks'] if any(sid in selected for sid in b['snippet_ids'])]
        incomplete=any(not b['complete'] for b in relevant)
        for block in relevant:
            if not block['complete']: continue
            positions=[ids.index(sid) for sid in block['snippet_ids'] if sid in ids]
            if not positions: continue
            idx=min(positions); ids=[sid for sid in ids if sid not in block['snippet_ids']]
            ids[idx:idx]=block['snippet_ids']
        added=[sid for sid in ids if sid not in selected]
        text=LABEL+'\n'+f"Model assessment (unverified): {answer.get('model_status',answer['status'])}.\n"
        if conclusions is not None:
            text += presentation.render(coverage['cited_conclusions']['operations'][rid])
        if reasons is not None:
            text += unknowns.render(coverage['unknown_explanations']['operations'][rid])
        text += ('\n'.join(f"[{sid}]" + (ADDED_LABEL if sid in added else '') + '\n'+available[sid]['quote'] for sid in ids) if ids else v20.NONE)
        if specific_unknown and reasons is None and answer.get('model_status', answer['status']) == 'unknown':
            text += '\nUnanswered request (model assessment, unverified): ' + row['request']
            text += '\nThe selected evidence does not establish this requested fact; this is not proof that no answer exists.'
        if incomplete: text += LIMITATION
        displayed += len(ids); chars += len(text)
        if displayed > MAX_PASSAGES or chars > MAX_TEXT:
            raise ValueError('Structural context exceeds extractive answer budget; no partial investigation saved')
        answer['text']=text; answer['snippet_ids']=ids; answer['source_excerpts']=[deepcopy(available[sid]) for sid in ids]
        answer['parts'][0]['claims']=[{'id':f'{rid}C{i}', 'status':'server-added structural context' if sid in added else 'source passage',
            'text':available[sid]['quote'],'snippet_ids':[sid],'source_excerpts':[deepcopy(available[sid])]} for i,sid in enumerate(ids,1)]
        changes[rid]={'model_selected_ids':selected,'server_added_ids':added,'blocks':deepcopy(relevant), 'incomplete':incomplete}
    coverage['structural_context']={'version':structural_trace.get('version', 'ordered-list-v1'),'operations':changes}
    return brief,calc,coverage


def validate_original(brief, coverage, snippets, trace, *, specific_unknown=False, unknown_mode=None, presentation_mode=None):
    groups=coverage.get('focus_groups', {})
    questions={q['id']:q['text'] for q in brief['questions']}
    plan=[{'id':rid,'request':questions[rid],'operation':'answer'} for rid in groups]
    payload={rid:{'groups':deepcopy(group['groups']),'more':group['model_reported_more']} for rid,group in groups.items()}
    if unknown_mode is not None:
        payload = unknowns.restore(payload, coverage.get('unknown_explanations'), version=unknown_mode)
    if presentation_mode is not None:
        payload = presentation.restore(payload, coverage.get('cited_conclusions'), version=presentation_mode)
    rebuilt,_,rebuilt_coverage=decode_results(json.dumps(payload),plan,snippets,[],[],structural_trace=trace, specific_unknown=specific_unknown, unknown_mode=unknown_mode, presentation_mode=presentation_mode)
    existing={a['question_id']:a for a in brief['answers']}
    if (rebuilt_coverage.get('cited_conclusions') != coverage.get('cited_conclusions') or
        rebuilt_coverage.get('unknown_explanations') != coverage.get('unknown_explanations') or
        rebuilt_coverage['structural_context'] != coverage.get('structural_context') or
        any(existing[a['question_id']] != a for a in rebuilt['answers'])):
        raise ValueError('Invalid original structural context')
