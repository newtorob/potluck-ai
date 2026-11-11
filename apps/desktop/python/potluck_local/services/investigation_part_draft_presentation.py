"""Model-draft display with exact source identity; not an entailment validator."""
from copy import deepcopy
import re
import unicodedata
from .investigation_part_basis_presentation import render as render_source_basis

ANSWER_MAX = 1000
UNKNOWN = 'Not established by the selected reviewed statements.'

def validate_draft(answer, verdict):
    if verdict not in ('supported','unknown','conflicting') or not isinstance(verdict,str):
        raise ValueError('Invalid draft verdict')
    if (not isinstance(answer,str) or not answer.strip() or len(answer)>ANSWER_MAX
            or any(unicodedata.category(c)=='Cc' for c in answer)
            or '```' in answer or re.search(r'\[\d+\]',answer)):
        raise ValueError('Invalid draft answer')
    if verdict=='unknown' and answer!=UNKNOWN:
        raise ValueError('Unknown draft must preserve selected-statement scope')
    return answer

def render(parts, request_context, *, max_visible=8000):
    # Reuse the immutable source renderer only for strict identity/role/dependency
    # validation. Its original source basis is preserved separately by the caller.
    source=render_source_basis(parts,request_context,max_visible=max_visible)
    citations=[{'number':i+1,**{key:deepcopy(value) for key,value in unit.items() if key!='text'}} for i,unit in enumerate(source['units'])]
    numbers={row['unit_id']:row['number'] for row in citations}
    def refs(ids):return ', '.join(f'[{numbers[uid]}]' for uid in ids)
    blocks=[]
    for part in source['parts']:
        answer=validate_draft(part.get('draft_answer'),part['verdict'])
        block=f"{part['id']}: {part['request']}\n{answer}"
        if part['verdict']=='conflicting':
            block+='\nSelected reviewed statements have unresolved incompatible alternatives; no authoritative winner is established.'
        roles=[(label,part[key]) for label,key in ((('Scope basis' if part['verdict']=='unknown' else 'Evidence'),'evidence_unit_ids'),('Context','context_unit_ids'),('Dependencies','dependency_only_unit_ids')) if part[key]]
        if roles:block+='\n'+'; '.join(label+': '+refs(ids) for label,ids in roles)
        blocks.append(block)
    if source['request_context']['unit_ids']:
        blocks.append('Request-wide source context: '+refs(source['request_context']['unit_ids']))
    text='\n\n'.join(blocks)
    if len(text)>max_visible:raise ValueError('Visible budget exceeded')
    return {'text':text,'parts':deepcopy(source['parts']),
            'request_context':deepcopy(source['request_context']),
            'answer_citations':citations,'source_basis_text':source['text']}
