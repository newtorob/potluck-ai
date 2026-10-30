import collections,json,pathlib,sys
w=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(w/'candidate-frozen/apps/desktop/python'))
from potluck_local.services import investigation_context,investigation_calculations
b=pathlib.Path('/Users/rob/Documents/potluck/bench/results/20261030-context-completeness-v1')
r=json.loads((b/'consumed-real-v11/attempts/core-company/raw/retrieval-001.json').read_text())['result']
evidence=r['source_chunks']+r['reviewed_claims'];remaining=24000
for e in evidence:
 assert e['kind']=='source_chunk'
 e['presented_content']=e['content'][:min(6000,remaining)];remaining-=len(e['presented_content'])
snippets,trace=investigation_context.snippets_from_evidence(evidence)
groups=collections.defaultdict(list)
for s in snippets:groups[s['citation_id']].append(s)
failures=[]
for e in evidence:
 group=groups[e['citation_id']];prefix=''.join(s['quote'] for s in group);source=e['content']
 if not source.startswith(prefix):
  mismatch=next((i for i,(a,c) in enumerate(zip(prefix,source)) if a!=c),min(len(prefix),len(source)))
  failures.append({'relative_path':e['relative_path'],'heading':e['heading'],'citation_id':e['citation_id'],'prefix_mismatch_offset':mismatch,'source_window':source[max(0,mismatch-40):mismatch+60],'concatenated_quotes_window':prefix[max(0,mismatch-40):mismatch+60],'every_quote_exact_substring':all(s['quote'] in source for s in group),'source_length':len(source),'concatenated_quote_length':len(prefix),'lost_characters':len(e['presented_content'])-len(prefix)})
try:
 investigation_calculations.quantity_catalog(snippets,source_context={e['citation_id']:e['content'] for e in evidence},version='v3')
 replay_error=None
except ValueError as ex: replay_error=str(ex)
report={'kind':'deterministic post-run diagnosis, no inference or source changes','method':'Reconstruct saved retrieval through unchanged 6000/source and24000total presentation bounds, candidate paragraph builder andv3 catalog.','replayed_error':replay_error,'paragraph_trace':trace,'failures':failures,'inference':'Whitespace-only blocks are skipped when an empty line is immediately followed by a list boundary, breaking the quantity parser requirement that ordered excerpts concatenate to a source prefix. Individual quotes remain exact. Needs a versioned fix and regression coverage before promotion.'}
(b/'integrity-consumed/context-failure-diagnosis.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
