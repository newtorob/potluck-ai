import { expect, it } from 'vitest';
import { roleMessages, roleResponseFormat, validateRole, validateRoleResult, type RoleJob } from '../src/agentHarnessRoles.js';

const job: RoleJob = {role:'test-matrix-v2',output:{version:'potluck-test-matrix-result-v2',maxWords:140,maxFindings:2},objective:'Propose checks',successCriteria:['Exercise the first condition.','Exercise the second condition.']};
const sources = {'@brain':'Context only.', 'contract.md':'Exact source rule.\n\nAnother exact paragraph.'};
const units = () => JSON.parse(roleMessages(job,sources).task).paragraphs as Array<{id:string;file:string;text:string}>;
const entry=(criterion:string,id:string)=>({criterion,setup:'Prepare the requested state.',action:'Perform the requested action.',expected:'Observe the outcome.',evidence_ids:[id]});
const make=()=>({status:'complete',checks:{C1:entry(job.successCriteria[0],units()[0].id),C2:entry(job.successCriteria[1],units()[1].id)},limitations:[] as string[]});

it('binds each exact criterion with the singleton enum schema and keeps it separate from generated findings',()=>{
 expect(validateRole(job.role,job.output).role).toBe(job.role);
 const schema:any=roleResponseFormat(job,sources);const properties=schema.schema.properties.checks.properties;
 expect(properties.C1.properties.criterion).toEqual({type:'string',enum:[job.successCriteria[0]]});
 expect(properties.C2.required[0]).toBe('criterion');
 const result=validateRoleResult(JSON.stringify(make()),job,sources);
 expect(result.matrixChecks?.map(c=>c.criterion)).toEqual(job.successCriteria);
 expect(result.findings[0].detail).not.toContain(job.successCriteria[0]);
 expect(result.findings[0].evidence[0].quote).toBe('Exact source rule.');
});
it('rejects altered echoes, duplicated keys, foreign checks and unbound evidence',()=>{
 const changed=make();changed.checks.C1.criterion+=' ';expect(()=>validateRoleResult(JSON.stringify(changed),job,sources)).toThrow('output_criterion_mismatch');
 const foreign:any=make();foreign.checks.C3=entry('other',units()[0].id);expect(()=>validateRoleResult(JSON.stringify(foreign),job,sources)).toThrow('output_fields');
 const bad=make();bad.checks.C1.evidence_ids=['foreign'];expect(()=>validateRoleResult(JSON.stringify(bad),job,sources)).toThrow('output_unbound_reference');
 const raw=JSON.stringify(make()).replace('"C2":','"C1":{},"C2":');expect(()=>validateRoleResult(raw,job,sources)).toThrow('output_duplicate_key');
 const v1:RoleJob={...job,role:'test-matrix-v1',output:{...job.output,version:'potluck-test-matrix-result-v1'}};
 const brain=JSON.parse(roleMessages({...job,role:'test-design-v3',output:{...job.output,version:'potluck-test-design-result-v3'}},sources).task).paragraphs.find((p:{file:string})=>p.file==='@brain');
 const brainValue=make();brainValue.checks.C1.evidence_ids=[brain.id];expect(()=>validateRoleResult(JSON.stringify(brainValue),job,sources)).toThrow('output_unbound_reference');
 expect(()=>validateRole(job.role,v1.output)).toThrow('unknown_output_contract');
});
it('excludes deterministic criterion from word budget while counting generated text and total bytes',()=>{
 const longJob={...job,successCriteria:['requirement '.repeat(150).trim()],output:{...job.output,maxFindings:1}};
 const id=JSON.parse(roleMessages(longJob,sources).task).paragraphs[0].id;
 const value={status:'complete',checks:{C1:entry(longJob.successCriteria[0],id)},limitations:[]};
 expect(validateRoleResult(JSON.stringify(value),longJob,sources).status).toBe('complete');
 value.checks.C1.expected='word '.repeat(141);expect(()=>validateRoleResult(JSON.stringify(value),longJob,sources)).toThrow('output_word_limit');
 expect(()=>validateRoleResult('x'.repeat(32769),longJob,sources)).toThrow('output_bytes');
});
it('keeps insufficient/sentinel rules and permits ordinary unresolved prose',()=>{
 const value=make();value.checks.C1.setup='Unresolved';expect(()=>validateRoleResult(JSON.stringify(value),job,sources)).toThrow('output_text');
 value.status='insufficient';value.checks.C1.evidence_ids=[];value.limitations=['Missing input.'];expect(validateRoleResult(JSON.stringify(value),job,sources).status).toBe('insufficient');
 value.limitations=[];expect(()=>validateRoleResult(JSON.stringify(value),job,sources)).toThrow('output_missing_limitation');
 const normal=make();normal.checks.C1.expected='The conflict remains unresolved.';expect(validateRoleResult(JSON.stringify(normal),job,sources).status).toBe('complete');
});
it('keeps v1 exact result shape and prompt/schema free of criterion echo',()=>{
 const v1:RoleJob={...job,role:'test-matrix-v1',output:{...job.output,version:'potluck-test-matrix-result-v1'}};
 const payload=make();delete (payload.checks.C1 as any).criterion;delete (payload.checks.C2 as any).criterion;
 const result=validateRoleResult(JSON.stringify(payload),v1,sources);
 expect(result.matrixChecks?.[0]).not.toHaveProperty('criterion');
 const schema:any=roleResponseFormat(v1,sources);expect(schema.schema.properties.checks.properties.C1.properties).not.toHaveProperty('criterion');
 expect(String(roleMessages(v1,sources).history[0].content)).not.toContain('copy its supplied criterion exactly');
});
