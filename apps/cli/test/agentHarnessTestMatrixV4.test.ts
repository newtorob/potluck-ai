import { mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, expect, it, vi } from 'vitest';
import { validatePlan, prepareHarness, runHarness, type HarnessPlan, type RunHarnessOptions } from '../src/agentHarness.js';
import { roleMessages, roleResponseFormat, validateRoleResult, type RoleJob } from '../src/agentHarnessRoles.js';

const job: RoleJob = {role:'test-matrix-v4',output:{version:'potluck-test-matrix-result-v4',maxWords:140,maxFindings:2},objective:'Propose checks',successCriteria:['Check first requirement.','Check second requirement.'],testRequirements:[{checkId:'C1',initialState:'An initial state.',actions:['Authored distinct action.'],expected:['Observe an outcome.']},{checkId:'C2',initialState:'Another initial state.',actions:['Perform another action.'],expected:['Observe another outcome.']}]};
const sources={'@brain':'Context only.', 'contract.md':'A general source rule.\n\nAnother source rule.'};
const units=()=>JSON.parse(roleMessages(job,sources).task).paragraphs as Array<{id:string;file:string;text:string}>;
const entry=(criterion:string,id:string)=>({criterion,setup:'Prepare a state.',action:'Perform an action.',expected:'Observe an outcome.',evidence_ids:[id]});
const response=()=>({status:'complete',checks:{C1:entry(job.successCriteria[0],units()[0].id),C2:entry(job.successCriteria[1],units()[1].id)},limitations:[] as string[]});
const plan=():HarnessPlan=>({version:'potluck-agent-plan-v3',id:'matrix',brain:'/tmp/brain.json',root:'/tmp',model:'fixture',contextWindowTokens:8192,limits:{concurrency:1,maxSteps:1,outputTokens:700,timeoutMs:2000,totalOutputTokens:700},jobs:[{id:'worker',...job,files:[{path:'contract.md',name:'contract.md'}]}]});

it('retains typed authored requirements in plan and user payload but never treats them as generated evidence',()=>{
 expect(validatePlan(plan()).jobs[0].testRequirements).toEqual(job.testRequirements);
 const task=JSON.parse(roleMessages(job,sources).task);expect(task.testRequirements).toEqual(job.testRequirements);expect(task.projectContext).toBe('Context only.');
 expect(task.paragraphs.every((p:{file:string})=>p.file!=='@brain')).toBe(true);
 const fmt:any=roleResponseFormat(job,sources);expect(fmt.schema.properties.checks.properties.C1.properties.criterion.enum).toEqual([job.successCriteria[0]]);
 const result=validateRoleResult(JSON.stringify(response()),job,sources);expect(result.matrixChecks?.[0].criterion).toBe(job.successCriteria[0]);
 expect(result.findings[0].detail).not.toContain(job.testRequirements![0].actions[0]);
 expect(result.findings[0].evidence[0].quote).toBe('A general source rule.');
});

it('rejects missing, reordered, duplicated, foreign, extra and unbounded requirement records',()=>{
 for(const value of [undefined,[],[job.testRequirements![0],job.testRequirements![0]],[job.testRequirements![1],job.testRequirements![0]],[job.testRequirements![0],{...job.testRequirements![1],checkId:'C3'}],[{...job.testRequirements![0],extra:'x'},job.testRequirements![1]],[{...job.testRequirements![0],initialState:''},job.testRequirements![1]],[{...job.testRequirements![0],actions:[]},job.testRequirements![1]],[{...job.testRequirements![0],expected:['x','y','z','w']},job.testRequirements![1]],[{...job.testRequirements![0],actions:['x'.repeat(401)]},job.testRequirements![1]]]){
  const p=plan();(p.jobs[0] as any).testRequirements=value;expect(()=>validatePlan(p)).toThrow();
 }
 const legacy=plan();legacy.jobs[0].role='test-matrix-v2';legacy.jobs[0].output={...job.output,version:'potluck-test-matrix-result-v2'};expect(()=>validatePlan(legacy)).toThrow();
});

it('requires exact echo, exact source binding, and preserves honest insufficient semantics',()=>{
 const changed=response();changed.checks.C1.criterion+=' ';expect(()=>validateRoleResult(JSON.stringify(changed),job,sources)).toThrow('output_criterion_mismatch');
 const bad=response();bad.checks.C1.evidence_ids=['foreign'];expect(()=>validateRoleResult(JSON.stringify(bad),job,sources)).toThrow('output_unbound_reference');
 const value=response();value.status='insufficient';value.checks.C1.setup='Unresolved';value.checks.C1.evidence_ids=[];value.limitations=['Source does not establish the request.'];
 expect(validateRoleResult(JSON.stringify(value),job,sources).status).toBe('insufficient');
 value.status='complete';expect(()=>validateRoleResult(JSON.stringify(value),job,sources)).toThrow('output_text');
});

it('leaves legacy v2 prompt, schema and result free of authored requirement data',()=>{
 const v2:RoleJob={...job,role:'test-matrix-v2',output:{...job.output,version:'potluck-test-matrix-result-v2'}};delete v2.testRequirements;
 expect(JSON.parse(roleMessages(v2,sources).task)).not.toHaveProperty('testRequirements');
 expect(roleResponseFormat(job,sources)).toEqual(roleResponseFormat(v2,sources));
 expect(validateRoleResult(JSON.stringify(response()),job,sources)).toEqual(validateRoleResult(JSON.stringify(response()),v2,sources));
});

let root='';afterEach(async()=>{if(root)await rm(root,{recursive:true,force:true});root='';});
it('real fake-runtime harness saves requirements in manifest and resumes without another call',async()=>{
 root=await realpath(await mkdtemp(join(tmpdir(),'requirements-')));const hub=join(root,'hub');
 await writeFile(join(root,'brain.json'),JSON.stringify({version:'potluck-agent-brain-v1',project:'sample',revision:'1',updatedAt:new Date(Date.now()-1000).toISOString(),expiresAt:new Date(Date.now()+3600000).toISOString(),brief:'Context.',priorities:['Review.'],constraints:['Read only.'],provenance:['Source.']}));
 await writeFile(join(root,'contract.md'),sources['contract.md']);const p=plan();p.root=root;p.brain=join(root,'brain.json');
 const execute=vi.fn<NonNullable<RunHarnessOptions['execute']>>(async(_history,task,options)=>{
  const data=JSON.parse(task);expect(data.testRequirements).toEqual(job.testRequirements);const id=data.paragraphs[0].id;
  options.callbacks.onToken(JSON.stringify({status:'complete',checks:{C1:entry(job.successCriteria[0],id),C2:entry(job.successCriteria[1],id)},limitations:[]}));return {status:'finished'};
 });
 const opts={endpoint:'http://127.0.0.1:31777/v1',apiKey:'fixture',execute};await prepareHarness(p,hub);
 expect((await runHarness(hub,p.id,opts)).jobs[0].status).toBe('needs_review');
 const manifest=JSON.parse(await readFile(join(hub,'batches',p.id,'manifest.json'),'utf8'));expect(manifest.plan.jobs[0].testRequirements).toEqual(job.testRequirements);
 const receipt=JSON.parse(await readFile(join(hub,'batches',p.id,'jobs/worker/receipt.json'),'utf8'));expect(receipt.outputCheck).toBe('passed_matrix_reference_checks_only');
 await runHarness(hub,p.id,opts);expect(execute).toHaveBeenCalledTimes(1);
});

it('preserves v3 messages and schema while separating assignment from policy in v4',()=>{
 const old:RoleJob={...job,role:'test-matrix-v3',output:{...job.output,version:'potluck-test-matrix-result-v3'}};
 expect(roleResponseFormat(job,sources)).toEqual(roleResponseFormat(old,sources));
 expect(roleMessages(old,sources).history[0].content).toContain('If the source does not support the requested behavior');
 const instruction=roleMessages(job,sources).history[0].content;
 expect(instruction).toContain('abstract test entrypoints and fixture preconditions');
 expect(instruction).toContain('Never treat a necessary condition alone as sufficient');
});
