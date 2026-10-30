#!/usr/bin/env python3
"""Create a self-contained, offline owner review packet from one raw benchmark run."""
import argparse
import hashlib
import html
import json
from pathlib import Path

DEFAULT_OUT = Path('/Users/rob/Documents/Codex/2026-10-27/https-www-ycombinator-com-rfs-https/outputs/potluck-owner-review.html')


def esc(value):
    return html.escape(str(value), quote=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', required=True, type=Path, help='Run arm containing raw.json')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    raw_path = args.run / 'raw.json'
    metadata_path = args.run / 'run-metadata.json'
    raw = json.loads(raw_path.read_text(encoding='utf-8'))
    metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.exists() else {}
    outputs = raw.get('outputs', {})
    # The packet must never include the other benchmark cases or any fixture/rubric.
    matching = [(key, value) for key, value in outputs.items() if 'pilot-readiness' in key.lower().replace('_', '-')]
    if len(matching) != 1:
        raise SystemExit('Expected exactly one pilot-readiness result in raw.json; found %d' % len(matching))
    case_id, attempt = matching[0]
    result = attempt.get('result') if attempt.get('status') == 'success' else None
    available = isinstance(result, dict) and isinstance(result.get('brief'), dict) and isinstance(result['brief'].get('answers'), list) and bool(result['brief']['answers'])
    digest = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest() if available else None
    provenance = result.get('provenance', {}) if available else {}
    snippets = provenance.get('snippet_catalog', []) if available else []
    answers = result['brief']['answers'] if available else []
    record = {'case_id': case_id, 'run_raw_sha256': hashlib.sha256(raw_path.read_bytes()).hexdigest(),
              'original_result_sha256_canonical_json': digest, 'original_result': result,
              'model': result.get('model_slug') if available else None,
              'model_sha256': provenance.get('model_sha256') or metadata.get('model_sha256'),
              'template_version': provenance.get('template_version') or raw.get('template_version'),
              'source_ids': [{'snippet_id': s.get('snippet_id'), 'citation_id': s.get('citation_id')} for s in snippets]}
    payload = json.dumps(record, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    answer_html = ''.join(f'<section class="answer"><h3>{esc(a.get("question_id", "Answer"))}</h3><p class="source">Cited snippet IDs: {esc(", ".join(a.get("snippet_ids", [])) or "None")}</p><pre>{esc(a.get("text", ""))}</pre><label>Proposed answer<textarea class="proposed" rows="5">{esc(a.get("text", ""))}</textarea></label></section>' for a in answers)
    cited_ids = {sid for answer in answers for sid in answer.get('snippet_ids', [])}
    paths = {e.get('citation_id'): e.get('relative_path') for e in result.get('evidence', []) if e.get('citation_id') and e.get('relative_path')} if available else {}
    def excerpts(entries):
        return ''.join(f'<article class="excerpt"><div class="source">{esc(s.get("snippet_id", ""))} · {esc(s.get("citation_id", ""))}{(" · " + esc(paths[s.get("citation_id")])) if s.get("citation_id") in paths else ""}</div><pre>{esc(s.get("quote", ""))}</pre></article>' for s in entries) or '<p>None saved.</p>'
    cited = [s for s in snippets if s.get('snippet_id') in cited_ids]
    other = [s for s in snippets if s.get('snippet_id') not in cited_ids]
    cited_html = excerpts(cited)
    other_html = excerpts(other)
    questions = ''.join(f'<p><strong>{esc(q.get("id", ""))}</strong> {esc(q.get("text", ""))}</p>' for q in result['brief'].get('questions', [])) if available else ''
    body = f'''<main><header><div class="eyebrow">POTLUCK · OWNER REVIEW</div><h1>Review the draft</h1><p class="lead">A diagnostic review of one saved model draft. This packet does not establish model quality or owner acceptance until you complete a review.</p><p class="meta">Case: {esc(case_id)} · Template: {esc(record['template_version'] or 'Unavailable')} · Model: {esc(record['model'] or 'Unavailable')}</p></header>'''
    if not available:
        body += f'<section class="panel"><h2>Draft unavailable</h2><p>The run did not produce a usable saved draft (status: {esc(attempt.get("status", "unknown"))}). There is nothing to accept or review.</p></section></main>'
    else:
        body += f'''<section class="panel timer-panel"><h2>Review timer</h2><p class="hint">The timer runs only after Start, while this page is visible and review is active. Pausing and hiding the page stop it. Resume to continue.</p><div class="timer"><strong id="elapsed">00:00</strong><button type="button" id="toggle">Start review</button></div><p class="hint">This standalone page does not upload or save to Potluck. Download the completed record to keep it.</p></section><div id="review-content" hidden><section class="panel"><h2>Question</h2>{questions or '<p>Question unavailable in the saved draft.</p>'}</section>
<section class="panel"><h2>Original answers</h2><p class="hint">The original text is preserved in the downloaded record. Edit the proposed answer only after starting your review.</p><fieldset id="review-fields" disabled>{answer_html}</fieldset></section>
<section class="panel"><h2>Your review</h2><fieldset id="decision-fields" disabled><label>Reviewer name<input id="reviewer" value="Rob" autocomplete="name"></label><label>Review notes<textarea id="notes" rows="4"></textarea></label><fieldset class="decisions"><legend>Decision</legend><label><input type="radio" name="decision" value="accept_unchanged"> Accept unchanged</label><label><input type="radio" name="decision" value="accept_with_edits"> Accept with edits</label><label><input type="radio" name="decision" value="reject"> Reject</label></fieldset><button type="button" id="finish">Finish review + Download JSON</button><p id="error" role="alert"></p></fieldset><p class="hint">The record is saved only when you download it. Closing this page loses unsaved review work.</p></section>
<section class="panel"><h2>Saved source excerpts</h2><p class="hint">These are the saved excerpts, not a claim that sources are complete or authoritative.</p><h3>Cited in the original answers ({len(cited)})</h3>{cited_html}<details><summary>Other retrieved excerpts ({len(other)})</summary>{other_html}</details></section></div><section class="panel" id="finished-panel" hidden><h2>Review finished</h2><p>Keep the downloaded JSON. This page does not upload or persist your review.</p><button type="button" id="download-again">Download review again</button></section></main>'''
    document = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>Potluck owner review</title><style>
:root{color-scheme:light; font-family:system-ui,-apple-system,sans-serif;color:#302820;background:#f3eee7}*{box-sizing:border-box}body{margin:0}main{max-width:840px;margin:auto;padding:48px 24px 96px}header{margin-bottom:32px}.eyebrow{font-size:12px;letter-spacing:.13em;font-weight:700;color:#935039}h1,h2,h3{font-family:Georgia,serif;font-weight:400}h1{font-size:46px;line-height:1.1;margin:12px 0}h2{font-size:27px;margin:0 0 12px}h3{font-size:20px;margin:0 0 8px}.lead{font-size:17px;line-height:1.55;max-width:650px}.meta,.hint,.source{color:#6b6055;font-size:13px;line-height:1.5}.source{overflow-wrap:anywhere;font-weight:600}.panel{background:#fffdf9;border:1px solid #ded4c9;border-radius:12px;padding:24px;margin:16px 0}.answer+.answer,.excerpt+.excerpt{border-top:1px solid #e9e0d7;margin-top:20px;padding-top:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.55;margin:8px 0 18px}label{display:block;font-size:14px;font-weight:600;margin:16px 0}textarea,input[type=text],input:not([type]){display:block;width:100%;margin-top:8px;border:1px solid #bbaea0;border-radius:6px;padding:10px;background:#fff;color:#302820;font:inherit}textarea{resize:none;line-height:1.5}.timer-panel{position:sticky;top:0;z-index:1}summary{cursor:pointer;font-weight:600;padding:12px 0}fieldset{border:0;margin:0;padding:0;min-width:0}.decisions{margin:16px 0}.decisions label{font-weight:400;margin:10px 0}.decisions input{margin-right:8px}button{border:1px solid #985038;border-radius:6px;background:#985038;color:white;padding:10px 15px;font:inherit;font-weight:600;cursor:pointer}button:hover{background:#793c29}button:focus-visible,input:focus-visible,textarea:focus-visible{outline:3px solid #b5754e;outline-offset:2px}button:disabled{opacity:.5;cursor:not-allowed}.timer{display:flex;align-items:center;gap:20px;margin:18px 0}.timer strong{font-variant-numeric:tabular-nums;font-size:20px}#error{color:#a32925}@media(max-width:480px){main{padding:30px 16px 60px}.panel{padding:18px}h1{font-size:38px}}
</style></head><body>''' + body + '''<script type="application/json" id="record">''' + payload + '''</script><script>
'use strict';
const record=JSON.parse(document.getElementById('record').textContent);
const toggle=document.getElementById('toggle');
if(toggle){
 const fields=[document.getElementById('review-fields'),document.getElementById('decision-fields')];
 const clock=document.getElementById('elapsed'), error=document.getElementById('error'), content=document.getElementById('review-content');
 let started=false, running=false, elapsed=0, since=0, startedAt=null, finished=false, captured=null;
 function stop(){if(running){elapsed+=performance.now()-since;running=false;} fields.forEach(f=>f.disabled=true);content.hidden=true;toggle.textContent=started?'Resume review':'Start review';}
 function draw(){const s=Math.floor((elapsed+(running?performance.now()-since:0))/1000);clock.textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');}
 toggle.addEventListener('click',()=>{if(finished)return;if(running){stop();}else if(!document.hidden){if(!started){started=true;startedAt=new Date().toISOString();}running=true;since=performance.now();fields.forEach(f=>f.disabled=false);content.hidden=false;toggle.textContent='Pause review';}draw();});
 window.addEventListener('beforeunload',event=>{if(started&&!finished){event.preventDefault();event.returnValue='';}});
 document.addEventListener('visibilitychange',()=>{if(document.hidden&&running){stop();draw();}});
 setInterval(draw,250);
 function download(){const blob=new Blob([JSON.stringify(captured,null,2)+'\\n'],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='potluck-owner-review.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
 document.getElementById('download-again').addEventListener('click',download);
 document.getElementById('finish').addEventListener('click',()=>{
  const decision=document.querySelector('input[name="decision"]:checked');const reviewer=document.getElementById('reviewer').value.trim();
  const proposed=Array.from(document.querySelectorAll('.proposed'),x=>x.value);
  const originals=record.original_result.brief.answers.map(a=>a.text||'');
  const changed=proposed.some((x,i)=>x!==originals[i]);
  if(!decision||!reviewer){error.textContent='Enter a reviewer name and choose a decision.';return;}
  if(decision.value==='accept_with_edits'&&proposed.some(x=>!x.trim())){error.textContent='Accepted answers cannot be blank.';return;}
  if(decision.value==='accept_unchanged'&&changed){error.textContent='The proposed answer has changed. Choose accept with edits, or restore the original text.';return;}
  if(decision.value==='accept_with_edits'&&!changed){error.textContent='Make an edit before choosing accept with edits.';return;}
  stop();finished=true;toggle.disabled=true;draw();error.textContent='';
  captured={...record,review:{reviewer,decision:decision.value,proposed_answers:proposed.map((text,i)=>({question_id:record.original_result.brief.answers[i].question_id,text})),notes:document.getElementById('notes').value,started_at:startedAt,finished_at:new Date().toISOString(),active_review_milliseconds:Math.round(elapsed),timing_scope:'Visible and explicitly active time only; hidden and paused time excluded.'}};
  document.getElementById('finished-panel').hidden=false;download();
 });
}
</script></body></html>'''
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(document, encoding='utf-8')
    print(args.output)

if __name__ == '__main__':
    main()
