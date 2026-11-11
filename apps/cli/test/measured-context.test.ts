import { expect, it } from 'vitest';
import { prepareContextMeasured, estimateContextTokens } from '../src/context.js';
import { compactVerification } from '../src/repair-feedback.js';
import type { ChatMessage } from '../src/gateway.js';
const pair = (id: string, text: string): ChatMessage[] => [
  {role:'assistant',content:'',tool_calls:[{id,type:'function',function:{name:'read_file',arguments:JSON.stringify({path:id})}}]},
  {role:'tool',tool_call_id:id,content:'TOOL_RESULT: FILE '+id+'\n'+text},
];
const count = async (messages: ChatMessage[]) => Math.ceil(JSON.stringify(messages).length / 3) + 350;
it('uses complete rendered counts instead of discarding fitting code by byte length', async () => {
  const history: ChatMessage[] = [{role:'system',content:'rules'},{role:'user',content:'fix target'},...pair('target','source '.repeat(1300))];
  const view = await prepareContextMeasured(history,{contextWindowTokens:8192,maxOutputTokens:2048,measureTokens:count});
  expect(view.messages).toBe(history);
  expect(estimateContextTokens(history)+2048).toBeGreaterThan(8192);
  expect(await count(view.messages)+view.maxOutputTokens).toBeLessThanOrEqual(8192);
});
it('retains active source ahead of recent unrelated output and preserves native pairs', async () => {
  const history: ChatMessage[] = [{role:'system',content:'rules'},{role:'user',content:'fix target'},
    ...pair('target','IMPORTANT_SOURCE '.repeat(100)),...pair('noise','unrelated '.repeat(700)),...pair('failure','latest failure')];
  const view = await prepareContextMeasured(history,{contextWindowTokens:2400,maxOutputTokens:500,measureTokens:count,priorityMessages:[history[3]]});
  expect(view.messages).toContain(history[3]);expect(view.messages).not.toContain(history[5]);
  expect(view.messages).toContain(history[7]);
  for(let i=0;i<view.messages.length;i++) if(view.messages[i].role==='tool') expect(view.messages[i-1].tool_calls?.[0].id).toBe(view.messages[i].tool_call_id);
  expect(await count(view.messages)+view.maxOutputTokens).toBeLessThanOrEqual(2400);
});
it('uses a safe estimate when count is explicitly unavailable, and propagates errors/cancellation', async () => {
  const history: ChatMessage[]=[{role:'system',content:'s'},{role:'user',content:'task'},...pair('large','x'.repeat(10000))];
  const view=await prepareContextMeasured(history,{contextWindowTokens:4096,measureTokens:async()=>undefined});
  expect(estimateContextTokens(view.messages)+view.maxOutputTokens).toBeLessThanOrEqual(4096);
  expect(view.messages.at(-1)?.content).toContain('partial view');
  await expect(prepareContextMeasured(history,{measureTokens:async()=>{throw new Error('changed model')}})).rejects.toThrow('changed model');
  const abort=new AbortController();abort.abort();
  await expect(prepareContextMeasured(history,{signal:abort.signal,measureTokens:count})).rejects.toThrow();
});
it('never trims instructions or task and marks truncated file results', async () => {
  const history:ChatMessage[]=[{role:'system',content:'RULES'},{role:'user',content:'TASK'},...pair('huge','x'.repeat(30000))];
  const original=structuredClone(history);
  const view=await prepareContextMeasured(history,{contextWindowTokens:2000,measureTokens:count});
  expect(view.messages).toContain(history[0]);expect(view.messages).toContain(history[1]);
  expect(view.messages.at(-1)?.content).toContain('partial view');
  expect(history).toEqual(original);expect(await count(view.messages)+view.maxOutputTokens).toBeLessThanOrEqual(2000);
});
it('keeps first failure and final status instead of only the tail of a long test log',()=>{
  const log='FAIL test_scaling\nAssertionError: expected 16 got 4\n'+ 'irrelevant progress\n'.repeat(600)+'FAILED (failures=1)';
  const result=compactVerification(log);
  expect(result).toContain('expected 16 got 4');expect(result).toContain('FAILED (failures=1)');
  expect(result).toContain('excerpt');expect(result.length).toBeLessThanOrEqual(3000);
});
it('counts native action metadata toward an explicit character cap', async () => {
  const history: ChatMessage[] = [{role:'user',content:'task'},...pair('x'.repeat(1500),'result')];
  await expect(prepareContextMeasured(history,{charBudget:500,measureTokens:async()=>100})).rejects.toThrow('cannot fit');
});
it('bounds Unicode verification excerpts without cutting surrogate pairs', () => {
  const excerpt = compactVerification('FAIL '+ '😀'.repeat(5000)+'\nFAILED');
  expect(excerpt.length).toBeLessThanOrEqual(3000);
  expect(excerpt).not.toMatch(/\p{Surrogate}/u);
});
