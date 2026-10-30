from pathlib import Path
import json,sys
w=Path(__file__).resolve().parent;repo=Path('/Users/rob/Documents/potluck');out=repo/'bench/results/20261030-planned-answers-v1'
sys.path.insert(0,str(w/'candidate-frozen/apps/desktop/python'))
from potluck_local.services import investigation_complete_answers as v11, investigation_planned_answers as v12
report={'scope':'Read-only isolated decoder diagnostics using saved prompts/replies. Snippet IDs/text are exact saved prompt values; citation IDs are placeholders, so this is not a source-binding audit. Does not repair or score invalid output.','cases':{}}
for version in ['v11','v12']:
 for folder in sorted((out/('8b-'+version)/'attempts').iterdir()):
  result=json.loads((folder/'raw/result.json').read_text())
  if result['status']!='error':continue
  entry={};calls=sorted((folder/'raw/calls').iterdir())
  try:
   if version=='v12':
    req=json.loads((calls[0]/'request.json').read_text());plan=json.loads(req['messages'][1]['content'])['operations'];reply=json.loads((calls[0]/'raw-reply.json').read_text())['choices'][0]['message']['content'];parts,_=v12.decode_plan(reply,plan);entry['plan_valid']=True
   final=calls[-1];req=json.loads((final/'request.json').read_text());payload=json.loads(req['messages'][1]['content']);raw=json.loads((final/'raw-reply.json').read_text())['choices'][0]['message']['content']
   data=json.loads(raw,object_pairs_hook=v12._unique_object);entry['strict_json_valid']=True
   snippets=[{'snippet_id':s['snippet_id'],'quote':s['text'],'citation_id':'source:00000000-0000-0000-0000-000000000001:00000000-0000-0000-0000-000000000002'} for s in payload['snippets']]
   for row in payload['operations']:
    if row['operation']!='answer':continue
    value=data[row['id']]
    if version=='v12':value={'parts':[{'request_span':p['request_span'],**value[p['id']]} for p in parts[row['id']]]}
    entry['text_lengths']=[len(p['text']) for p in value['parts']]
    entry['aggregate_chars_with_overhead']=sum(entry['text_lengths'])+2*len(entry['text_lengths'])
    v11._parts(value,row['request'],snippets)
  except Exception as e:entry['replayed_rejection']={'type':type(e).__name__,'message':str(e)}
  report['cases'][version+'/'+folder.name]=entry
(out/'integrity/decoder-replay.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
