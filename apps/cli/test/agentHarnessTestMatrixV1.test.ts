import { mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, expect, it, vi } from 'vitest';
import { roleMessages, roleResponseFormat, validateRole, validateRoleResult, type RoleJob } from '../src/agentHarnessRoles.js';
import { prepareHarness, runHarness, type HarnessPlan, type RunHarnessOptions } from '../src/agentHarness.js';

const job: RoleJob = {role:'test-matrix-v1',output:{version:'potluck-test-matrix-result-v1',maxWords:140,maxFindings:3},objective:'Propose checks',successCriteria:['Check save.','Check correction.','Check replay.']};
const sources = {'@brain':'Context only.', 'contract.md':'A complete rule.\n\nA second exact paragraph.'};
const refs = () => JSON.parse(roleMessages(job,sources).task).paragraphs as Array<{id:string,file:string,text:string}>;
const entry = (id:string) => ({setup:'Prepare a valid record.',action:'Perform the requested operation.',expected:'Observe the expected outcome.',evidence_ids:[id]});
const make = () => ({status:'complete',checks:{C1:entry(refs()[0].id),C2:entry(refs()[0].id),C3:entry(refs()[1].id)},limitations:[] as string[]});

it('requires every authored criterion and materializes exact source references in distinct check metadata',()=>{
 expect(validateRole(job.role,job.output).role).toBe(job.role);
 const packet=roleMessages(job,sources),task=JSON.parse(packet.task);
 expect(task.checks).toEqual([{id:'C1',description:'Check save.'},{id:'C2',description:'Check correction.'},{id:'C3',description:'Check replay.'}]);
 expect(task.projectContext).toBe('Context only.');
 expect(task.paragraphs.every((p:{file:string})=>p.file!=='@brain')).toBe(true);
 const result=validateRoleResult(JSON.stringify(make()),job,sources);
 expect(result.matrixChecks?.map(c=>c.check_id)).toEqual(['C1','C2','C3']);
 expect(result.findings.map(f=>f.detail.split('\n')[0])).toEqual(['Check C1','Check C2','Check C3']);
 expect(result.findings[0].evidence[0]).toEqual({file:'contract.md',quote:'A complete rule.',unit_id:refs()[0].id});
 expect(result.findings[1].evidence[0]).toEqual(result.findings[0].evidence[0]);
 const schema:any=roleResponseFormat(job,sources); expect(schema.schema.properties.checks.required).toEqual(['C1','C2','C3']);
 expect(JSON.stringify(schema)).not.toContain('"anyOf"');
});

it('rejects missing, foreign and duplicated check keys, references, criteria and extra fields',()=>{
 const val=make();const missing=structuredClone(val);delete (missing.checks as any).C2;
 expect(()=>validateRoleResult(JSON.stringify(missing),job,sources)).toThrow('output_fields');
 const extra:any=structuredClone(val);extra.checks.C4=entry(refs()[0].id);
 expect(()=>validateRoleResult(JSON.stringify(extra),job,sources)).toThrow('output_fields');
 const foreign=structuredClone(val);foreign.checks.C1.evidence_ids=['foreign'];
 expect(()=>validateRoleResult(JSON.stringify(foreign),job,sources)).toThrow('output_unbound_reference');
 const duplicate=structuredClone(val);duplicate.checks.C1.evidence_ids=[refs()[0].id,refs()[0].id];
 expect(()=>validateRoleResult(JSON.stringify(duplicate),job,sources)).toThrow('output_unbound_reference');
 expect(()=>validateRoleResult(JSON.stringify(val).replace('"C2":','"C1":{},"C2":'),job,sources)).toThrow('duplicate_key');
 expect(()=>roleResponseFormat({...job,successCriteria:['a','b','c','d']},sources)).toThrow('matrix_criteria_limit');
 expect(()=>roleResponseFormat({...job,output:{...job.output,maxFindings:2}},sources)).toThrow('matrix_criteria_limit');
 expect(()=>validateRoleResult(JSON.stringify(val),job,{'contract.md':'Changed source.'})).toThrow('output_unbound_reference');
 const brainOnly={'@brain':'Context only.'};expect(()=>roleMessages(job,brainOnly)).toThrow('matrix_missing_source');
});

it('supports honest insufficient results while enforcing complete fields and bounded output',()=>{
 const x=make();x.checks.C1.setup='';expect(()=>validateRoleResult(JSON.stringify(x),job,sources)).toThrow('output_text');
 const sentinel=make();sentinel.checks.C1.expected='Unresolved';expect(()=>validateRoleResult(JSON.stringify(sentinel),job,sources)).toThrow('output_text');
 const ordinary=make();ordinary.checks.C1.expected='The conflict remains unresolved.';expect(validateRoleResult(JSON.stringify(ordinary),job,sources).status).toBe('complete');
 x.status='insufficient';x.checks.C1.setup='Unresolved';x.checks.C1.action='Unresolved';x.checks.C1.expected='Unresolved';x.checks.C1.evidence_ids=[];x.limitations=['Missing input.'];
 expect(validateRoleResult(JSON.stringify(x),job,sources).matrixChecks?.[0].setup).toBe('Unresolved');
 x.limitations=[];expect(()=>validateRoleResult(JSON.stringify(x),job,sources)).toThrow('output_missing_limitation');
 const long=make();long.checks.C2.expected='word '.repeat(141);expect(()=>validateRoleResult(JSON.stringify(long),job,sources)).toThrow('output_word_limit');
 expect(()=>validateRoleResult('x'.repeat(32769),job,sources)).toThrow('output_bytes');
 const huge={'contract.md':'x'.repeat(32769)},id=JSON.parse(roleMessages(job,huge).task).paragraphs[0].id;
 const v=make();for(const check of Object.values(v.checks))check.evidence_ids=[id];
 expect(()=>validateRoleResult(JSON.stringify(v),job,huge)).toThrow('output_evidence_bytes');
});

let root='';afterEach(async()=>{if(root)await rm(root,{recursive:true,force:true});root='';});
it('real harness persists and resumes a matrix result without another fake runtime call',async()=>{
 root=await realpath(await mkdtemp(join(tmpdir(),'potluck-matrix-')));const hub=join(root,'hub');
 await writeFile(join(root,'brain.json'),JSON.stringify({version:'potluck-agent-brain-v1',project:'sample',revision:'1',updatedAt:new Date(Date.now()-1000).toISOString(),expiresAt:new Date(Date.now()+3600000).toISOString(),brief:'Context.',priorities:['Review.'],constraints:['Read only.'],provenance:['Source.']}));
 await writeFile(join(root,'contract.md'),sources['contract.md']);
 const plan: HarnessPlan={version:'potluck-agent-plan-v3',id:'matrix-test',brain:join(root,'brain.json'),root,model:'fixture',contextWindowTokens:8192,limits:{concurrency:1,maxSteps:1,outputTokens:700,timeoutMs:2000,totalOutputTokens:700},jobs:[{id:'worker',...job,files:[{path:'contract.md',name:'contract.md'}]}]};
 const execute=vi.fn<NonNullable<RunHarnessOptions['execute']>>(async(_history,task,options)=>{
  const taskData=JSON.parse(task),id=taskData.paragraphs[0].id;
  expect(options.responseFormat?.type).toBe('json_object');
  options.callbacks.onToken(JSON.stringify({status:'complete',checks:{C1:entry(id),C2:entry(id),C3:entry(id)},limitations:[]}));return {status:'finished'};
 });
 const opts={endpoint:'http://127.0.0.1:31777/v1',apiKey:'fixture',execute};await prepareHarness(plan,hub);
 expect((await runHarness(hub,plan.id,opts)).jobs[0].status).toBe('needs_review');
 const receipt=JSON.parse(await readFile(join(hub,'batches',plan.id,'jobs/worker/receipt.json'),'utf8'));
 expect(receipt).toMatchObject({needsPrimaryReview:true,outputCheck:'passed_matrix_reference_checks_only'});
 expect(receipt.result.matrixChecks).toHaveLength(3);await runHarness(hub,plan.id,opts);expect(execute).toHaveBeenCalledTimes(1);
});


it('brain remains visible but cannot consume paragraph budget or become evidence',()=>{
 const source = Array.from({length:128},(_,i)=>`Source paragraph ${i}.`).join('\n\n');
 const task=JSON.parse(roleMessages(job,{'@brain':'Context only.', 'contract.md':source}).task);
 expect(task.paragraphs).toHaveLength(128);expect(task.projectContext).toBe('Context only.');
 const v3={...job,role:'test-design-v3' as const,output:{...job.output,version:'potluck-test-design-result-v3' as const}};
 const brain=JSON.parse(roleMessages(v3,sources).task).paragraphs.find((p:{file:string})=>p.file==='@brain');
 const value=make();value.checks.C1.evidence_ids=[brain.id];
 expect(()=>validateRoleResult(JSON.stringify(value),job,sources)).toThrow('output_unbound_reference');
});
