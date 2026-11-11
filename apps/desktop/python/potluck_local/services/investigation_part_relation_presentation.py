"""Display validated exact source passages once, retaining all part roles; not a semantic validator."""
from copy import deepcopy


def render(parts, request_context, *, max_visible=8000):
    original=deepcopy(parts);context=deepcopy(request_context)
    if type(max_visible) is not int or max_visible<0:raise ValueError('Invalid limit')
    units={}; order=[]; seen_parts=set()
    def collect(passages, ids):
        if not isinstance(passages,list) or [p['unit_id'] for p in passages]!=ids:
            raise ValueError('Passage binding mismatch')
        for p in passages:
            key=p['unit_id']; identity=(p['citation_id'],p['ranges'],p['text'])
            if key in units and units[key]!=identity:raise ValueError('Unit identity changed')
            if key not in units:units[key]=identity;order.append(key)
    for part in original:
        if part['id'] in seen_parts:raise ValueError('Duplicate part')
        seen_parts.add(part['id'])
        if part['verdict'] not in ('supported','unknown','conflicting'):raise ValueError('Invalid verdict')
        answer=part['answer_unit_ids']; alternative=part['alternative_unit_ids']
        if alternative or (part['verdict']=='unknown' and answer) or (part['verdict']!='unknown' and not answer):
            raise ValueError('Invalid explicit relation evidence')
        ids=part['unit_ids'];collect(part['passages'],ids)
        requested=part['requested_unit_ids'];deps=part['dependency_only_unit_ids']
        roles=part['answer_unit_ids']+part['alternative_unit_ids']+part['context_unit_ids']
        if len(set(ids))!=len(ids) or len(set(roles))!=len(roles) or set(roles)!=set(requested) or set(ids)!=set(requested)|set(deps) or set(requested)&set(deps):raise ValueError('Invalid role or dependency binding')
    collect(context['passages'],context['unit_ids'])
    grouped=[]
    for uid in order:
        citation,ranges,text=units[uid]
        if (grouped and len(ranges)==1 and len(grouped[-1]['range'])==1
                and grouped[-1]['citation']==citation and grouped[-1]['range'][0][1]==ranges[0][0]):
            grouped[-1]['ids'].append(uid);grouped[-1]['range'][0][1]=ranges[0][1];grouped[-1]['text']+=text
        else:
            grouped.append({'ids':[uid],'citation':citation,'range':deepcopy(ranges),'text':text})
    numbers={uid:i+1 for i,group in enumerate(grouped) for uid in group['ids']}
    def refs(ids):return ', '.join(f'[{number}]' for number in dict.fromkeys(numbers[x] for x in ids)) or 'none'
    blocks=[]
    for part in original:
        lead=f"{part['id']}: {part['request']}"
        if part['verdict']=='unknown':lead+=' — Not established by the selected reviewed statements.'
        if part['verdict']=='conflicting':lead+=' — Selected reviewed statements have unresolved incompatible alternatives; no authoritative winner is established.'
        roles=[(label,part[key]) for label,key in (('Answer','answer_unit_ids'),('Alternative','alternative_unit_ids'),('Context','context_unit_ids'),('Dependencies','dependency_only_unit_ids')) if part[key]]
        blocks.append(lead+('\n'+'; '.join(label+': '+refs(ids) for label,ids in roles) if roles else ''))
    if context['unit_ids']:blocks.append('Request-wide context: '+refs(context['unit_ids']))
    blocks += [f'[{i+1}]\n{group["text"]}' for i,group in enumerate(grouped)]
    text='\n\n'.join(blocks)
    if len(text)>max_visible:raise ValueError('Visible budget exceeded')
    return {'text':text,'parts':original,'request_context':context,'units':[{'unit_id':uid,'citation_id':units[uid][0],'ranges':deepcopy(units[uid][1]),'text':units[uid][2]} for uid in order]}
