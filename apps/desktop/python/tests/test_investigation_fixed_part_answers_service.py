"""Synthetic persistence and currentness adversarial checks; no native inference."""
import asyncio
from copy import deepcopy
import json
import sqlite3

import pytest

from test_investigation_reviewed_claim_selection_v16_service import setup, Runtime, QUESTION
from potluck_local.services import investigation_request_parts as part_plans
from potluck_local.services import investigations as inv
from potluck_local.services import reviewed_claim_unit_plans as unit_plans
from potluck_local.services import investigation_verification as verification
from potluck_local.routers import investigations as routes

MODE = 'fixed-part-selection-v1'


def make_plan():
    return part_plans.prepare(QUESTION, ['What is required for access?', 'What is the exact expiry date?'],
                              origin='fixture_supplied', request=QUESTION)


class FixedRuntime(Runtime):
    def __init__(self, *, failure=None, change=None, overflow=None):
        super().__init__(change=change)
        self.failure = failure
        self.overflow = overflow

    async def measure_investigation_prompt(self, messages, **kwargs):
        result = await super().measure_investigation_prompt(messages, **kwargs)
        if len(self.measurements) == self.overflow:
            return {'input_tokens': 8100, 'context_tokens': 8192}
        return result

    async def chat(self, messages, **kwargs):
        self.calls.append(deepcopy((messages, kwargs)))
        stage = len(self.calls)
        if self.change and stage == 1:
            await asyncio.wait_for(self.change(), timeout=2)
        if self.failure == stage:
            value = '{"P1":[],"P1":[],"P2":[]}' if stage == 1 else '{"P1":{"verdict":"supported","unit_ids":["C1:rule"]}}'
        elif stage == 1:
            value = json.dumps({'P1':['C1:rule'], 'P2':[]})
        else:
            value = json.dumps({'P1':{'verdict':'supported','unit_ids':['C1:rule']},
                                'P2':{'verdict':'unknown','unit_ids':[]}})
        return {'choices':[{'message':{'content':value},'finish_reason':'stop'}]}


async def create(scope, plan=None):
    return await inv.create_investigation(scope, QUESTION, 'Access', 'model',
        template_version='investigation-v21',
        reviewed_operations=[{'request':QUESTION,'operation':'answer'}],
        evidence_selection='reviewed-current-v1', presentation_mode=MODE,
        generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1',
        reliability_mode='bounded-evidence-v1', question_part_plan=make_plan() if plan is None else plan)


def counts(path):
    with sqlite3.connect(path) as db:
        return (db.execute('SELECT COUNT(*) FROM investigations').fetchone()[0],
                db.execute('SELECT COUNT(*) FROM investigation_bindings').fetchone()[0])


def test_success_replay_correction_export_and_unsupported_verification(tmp_path, monkeypatch):
    async def run():
        _,scope,_,_,_=await setup(tmp_path,monkeypatch);runtime=FixedRuntime()
        monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        item=await create(scope);answer=item['brief']['answers'][0]
        assert answer['status']=='unknown' and 'Approval is required.' in answer['text']
        assert 'Suspended members are excluded.' in answer['text'] and 'expiry' in answer['text'].lower()
        assert len(runtime.calls)==2 and [call[1]['max_tokens'] for call in runtime.calls]==[2000,512]
        assert (await inv.get_investigation(scope,item['id']))['brief']==item['brief']
        with pytest.raises(ValueError,match='Automatic verification is unsupported'):
            verification.original_input(item)
        exported=(await routes.export(item['id'],scope.workspace,list(scope.collection_ids)))['markdown']
        assert r'Suspended members are excluded\.' in exported
        corrected=deepcopy(item['brief']);corrected['answers'][0]['text']='Approval is required; suspended members are excluded; the selected statements do not specify expiry.'
        reviewed=await inv.review_investigation(scope,item['id'],'correct','reviewer','Synthetic correction',corrected_brief=corrected)
        assert reviewed['brief']==item['brief']
    asyncio.run(run())


@pytest.mark.parametrize('paragraphs', [0, 20])
def test_exact_partition_correction_preserves_original_and_normalizes_blank_citations(tmp_path, monkeypatch, paragraphs):
    import test_investigation_reviewed_claim_selection_v16_service as source_fixture

    async def run():
        details = ''.join(f'\n\n- Access requirement detail {i}.' for i in range(paragraphs))
        monkeypatch.setattr(source_fixture, 'BODY', '## Access\nApproval is required.\n\n```text\naccess_example\n```\n' + details
                            + '\n\nSuspended members are excluded.\n\nUnrelated retention is 30 days.')
        path, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        runtime = FixedRuntime()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        original = await create(scope)
        answer = original['brief']['answers'][0]
        blanks = [s for s in answer['source_excerpts'] if not s['quote'].strip()]
        assert blanks
        if paragraphs:
            assert sum(bool(s['quote'].strip()) for s in answer['source_excerpts']) > 16
        reviewed = await inv.review_investigation(scope, original['id'], 'correct', 'reviewer',
            'Synthetic exact partition correction', corrected_brief=deepcopy(original['brief']))
        corrected = reviewed['review']['corrected_brief']['answers'][0]
        assert all(s['quote'].strip() for s in corrected['source_excerpts'])
        assert 'passage_presentation' not in corrected
        assert reviewed['brief'] == original['brief'] and reviewed['provenance'] == original['provenance']
        await routes.export(original['id'], scope.workspace, list(scope.collection_ids))
        outcome = await inv.record_outcome(scope, original['id'], 'reviewer', 'inconclusive', 'Synthetic outcome')
        reopened = await inv.get_investigation(scope, original['id'])
        assert outcome['brief'] == reopened['brief'] == original['brief']
        assert outcome['provenance'] == reopened['provenance'] == original['provenance']
        assert len(runtime.calls) == 2
        # Stored corrections must retain the canonical nonblank projection.
        altered = deepcopy(reviewed['review'])
        altered['corrected_brief']['answers'][0]['snippet_ids'].append(blanks[0]['snippet_id'])
        altered['corrected_brief']['answers'][0]['source_excerpts'].append(blanks[0])
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET review=? WHERE id=?', (json.dumps(altered), original['id']))
        with pytest.raises((ValueError, LookupError)):
            await inv.get_investigation(scope, original['id'])
    asyncio.run(run())


@pytest.mark.parametrize('stage',[1,2])
def test_invalid_part_response_never_retries_or_saves(tmp_path,monkeypatch,stage):
    async def run():
        path,scope,_,_,_=await setup(tmp_path,monkeypatch);runtime=FixedRuntime(failure=stage)
        monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        with pytest.raises((ValueError,inv.InvestigationInferenceError)):await create(scope)
        assert len(runtime.calls)==stage and counts(path)==(0,0)
    asyncio.run(run())


@pytest.mark.parametrize('stage',[1,2])
def test_measurement_overflow_never_dispatches_that_stage(tmp_path,monkeypatch,stage):
    async def run():
        path,scope,_,_,_=await setup(tmp_path,monkeypatch);runtime=FixedRuntime(overflow=stage)
        monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        with pytest.raises((ValueError,inv.InvestigationInferenceError)):await create(scope)
        assert len(runtime.measurements)==stage and len(runtime.calls)==stage-1 and counts(path)==(0,0)
    asyncio.run(run())


def test_cancellation_between_stages_never_saves_or_retries(tmp_path,monkeypatch):
    async def run():
        path,scope,_,_,_=await setup(tmp_path,monkeypatch)
        entered=asyncio.Event();release=asyncio.Event()
        class Blocked(FixedRuntime):
            async def chat(self,messages,**kwargs):
                result=await super().chat(messages,**kwargs)
                if len(self.calls)==1:
                    entered.set();await release.wait()
                return result
        runtime=Blocked();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        task=asyncio.create_task(create(scope));await asyncio.wait_for(entered.wait(),2);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert len(runtime.calls)==1 and counts(path)==(0,0)
    asyncio.run(run())


@pytest.mark.parametrize('tamper',['missing_plan','rehashed_plan','rehashed_both','strip_markers','strip_all_storage','storage_marker','raw'])
def test_persisted_plan_and_independent_marker_tampering_fails(tmp_path,monkeypatch,tamper):
    async def run():
        path,scope,_,_,_=await setup(tmp_path,monkeypatch)
        monkeypatch.setattr(inv,'get_runtime',FixedRuntime);item=await create(scope)
        provenance=deepcopy(item['provenance'])
        if tamper=='missing_plan':del provenance['question_part_plan']
        elif tamper in ('rehashed_plan','rehashed_both'):
            provenance['question_part_plan']=part_plans.prepare(QUESTION,['Different rule?','What is the exact expiry date?'],origin='fixture_supplied',request=QUESTION)
        elif tamper in ('strip_markers','strip_all_storage'):
            for name in ('presentation_mode','question_part_plan','reviewed_claim_selection','reviewed_claim_selection_input','reviewed_claim_review_input','reviewed_unit_plan_receipts'):
                provenance.pop(name,None)
        elif tamper=='raw':provenance['reviewed_claim_selection']['raw']='{}'
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET provenance=? WHERE id=?',(json.dumps(provenance),item['id']))
            if tamper=='storage_marker':db.execute('UPDATE investigations SET question_part_contract=NULL WHERE id=?',(item['id'],))
            elif tamper=='rehashed_both':db.execute('UPDATE investigations SET question_part_contract=? WHERE id=?',(json.dumps({'version':MODE,'plan':provenance['question_part_plan']}),item['id']))
            elif tamper=='strip_all_storage':db.execute('UPDATE investigations SET question_part_contract=NULL,unit_plan_contract=NULL WHERE id=?',(item['id'],))
        with pytest.raises((ValueError,LookupError)):await inv.get_investigation(scope,item['id'])
    asyncio.run(run())


def test_currentness_change_during_first_call_fails_before_save(tmp_path,monkeypatch):
    async def run():
        path,scope,claim,plan,old=await setup(tmp_path,monkeypatch)
        async def change():
            replacement=await unit_plans.propose_plan('w',scope.collection_ids,claim,plan)
            await unit_plans.supersede_plan('w',scope.collection_ids,old,replacement,'reviewer','Synthetic replacement')
        runtime=FixedRuntime(change=change);monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        with pytest.raises(LookupError):await create(scope)
        assert len(runtime.calls)==2 and counts(path)==(0,0)
    asyncio.run(run())


@pytest.mark.parametrize('operation',['accept','correct','reject','outcome'])
def test_currentness_race_after_read_blocks_review_and_outcome(tmp_path,monkeypatch,operation):
    async def run():
        path,scope,claim,plan,old=await setup(tmp_path,monkeypatch)
        runtime=FixedRuntime();monkeypatch.setattr(inv,'get_runtime',lambda:runtime);item=await create(scope)
        if operation=='outcome':await inv.review_investigation(scope,item['id'],'accept','reviewer','Synthetic acceptance')
        get=inv.get_investigation;changed=False
        async def stale_read(*args,**kwargs):
            nonlocal changed
            record=await get(*args,**kwargs)
            if not changed:
                changed=True;replacement=await unit_plans.propose_plan('w',scope.collection_ids,claim,plan)
                await unit_plans.supersede_plan('w',scope.collection_ids,old,replacement,'reviewer','Synthetic race')
            return record
        monkeypatch.setattr(inv,'get_investigation',stale_read)
        with pytest.raises(LookupError):
            if operation=='outcome':await inv.record_outcome(scope,item['id'],'reviewer','resolved','Synthetic outcome')
            else:await inv.review_investigation(scope,item['id'],operation,'reviewer','Synthetic review',corrected_brief=deepcopy(item['brief']) if operation=='correct' else None)
        with sqlite3.connect(path) as db:
            status,review,outcome=db.execute('SELECT status,review,outcome FROM investigations WHERE id=?',(item['id'],)).fetchone()
        assert outcome is None and status==('reviewed' if operation=='outcome' else 'draft')
        assert (review is not None)==(operation=='outcome') and len(runtime.calls)==2
    asyncio.run(run())


def test_exact_budget_and_schema_receipts_then_accept_outcome(tmp_path, monkeypatch):
    async def run():
        path, scope, claim, plan, old = await setup(tmp_path, monkeypatch)
        runtime = FixedRuntime()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        item = await create(scope)
        assert item['status'] == 'draft' and item['review'] is None
        assert item['provenance']['question_part_plan'] == make_plan()
        assert 'question_part_contract' not in item and 'unit_plan_contract' not in item
        assert item['brief']['answers'][0]['passage_presentation']['needs_review'] is True
        for measured, dispatched in zip(runtime.measurements, runtime.calls):
            assert measured[0] == dispatched[0]
            assert measured[1]['response_format'] == dispatched[1]['response_format']
            assert set(dispatched[1]['response_format']['schema']['properties']) == {'P1', 'P2'}
            assert dispatched[1]['expected_context_tokens'] == 8192
            assert dispatched[1]['expected_model'] == 'model'
            assert dispatched[1]['expected_sha256'] == 'a' * 64
        first = item['provenance']['reviewed_claim_selection_input']
        assert first['coverage'] == 'not_verified' and len(first['complete_input_sha256']) == 64
        assert 'sources' in item['evidence'][0]  # Full support remains saved, outside model messages.
        reviewed = await inv.review_investigation(scope, item['id'], 'accept', 'fixture reviewer', 'Synthetic acceptance')
        assert reviewed['status'] == 'reviewed'
        final = await inv.record_outcome(scope, item['id'], 'fixture reviewer', 'inconclusive', 'Synthetic outcome')
        assert final['outcome']['result'] == 'inconclusive' and len(runtime.calls) == 2
        replacement = await unit_plans.propose_plan('w', scope.collection_ids, claim, plan)
        await unit_plans.supersede_plan('w', scope.collection_ids, old, replacement, 'fixture reviewer', 'Synthetic replacement')
        with pytest.raises(LookupError):
            await inv.get_investigation(scope, item['id'])
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await routes.export(item['id'], scope.workspace, list(scope.collection_ids))
        assert await inv.list_investigations(scope) == []
    asyncio.run(run())


@pytest.mark.parametrize('invalid', ['no_plan', 'other_mode', 'question', 'request', 'origin', 'digest', 'credential', 'calculation'])
def test_invalid_plan_or_options_fail_before_measurement(tmp_path, monkeypatch, invalid):
    async def run():
        _, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        runtime = FixedRuntime()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        plan, mode, operation = make_plan(), MODE, 'answer'
        if invalid == 'no_plan': plan = None
        elif invalid == 'other_mode': mode = 'reviewed-claim-selection-v16'
        elif invalid == 'question': plan = part_plans.prepare('Other question?', ['Other?'], origin='fixture_supplied', request=QUESTION)
        elif invalid == 'request': plan = part_plans.prepare(QUESTION, ['Other?'], origin='fixture_supplied', request='Other request?')
        elif invalid == 'origin': plan['origin'] = 'approved'
        elif invalid == 'digest': plan['plan_sha256'] = '0' * 64
        elif invalid == 'credential':
            plan = part_plans.prepare(QUESTION, ['sk-proj-' + 'Synthetic09_' * 4], origin='fixture_supplied')
        elif invalid == 'calculation': operation = 'difference'
        with pytest.raises(ValueError):
            await inv.create_investigation(scope, QUESTION, 'Access', 'model',
                template_version='investigation-v21', reviewed_operations=[{'request': QUESTION, 'operation': operation}],
                evidence_selection='reviewed-current-v1', presentation_mode=mode,
                generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1',
                reliability_mode='bounded-evidence-v1', question_part_plan=plan)
        assert runtime.calls == runtime.measurements == []
    asyncio.run(run())


@pytest.mark.parametrize('tamper', ['budget_bool', 'second_capacity', 'prompt_hash', 'schema_hash', 'coverage', 'answer', 'reviewed_part'])
def test_original_receipt_and_answer_tampering_blocks_export(tmp_path, monkeypatch, tamper):
    async def run():
        path, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', FixedRuntime)
        item = await create(scope)
        p, brief = deepcopy(item['provenance']), deepcopy(item['brief'])
        if tamper == 'budget_bool': p['input_budget']['input_tokens'] = True
        elif tamper == 'second_capacity': p['reviewed_claim_review_input']['context_tokens'] += 1
        elif tamper == 'prompt_hash': p['reviewed_claim_selection_input']['messages_sha256'] = '0' * 64
        elif tamper == 'schema_hash': p['reviewed_claim_review_input']['schema_sha256'] = '0' * 64
        elif tamper == 'coverage': p['question_part_plan']['coverage'] = 'verified'
        elif tamper == 'answer': brief['answers'][0]['text'] = 'Access requires no approval.'
        else: p['reviewed_claim_selection']['review']['parts'][0]['verdict'] = 'unknown'
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET brief=?,provenance=? WHERE id=?', (json.dumps(brief), json.dumps(p), item['id']))
        with pytest.raises(ValueError): await inv.get_investigation(scope, item['id'])
        from fastapi import HTTPException
        with pytest.raises(HTTPException): await routes.export(item['id'], scope.workspace, list(scope.collection_ids))
    asyncio.run(run())


def test_self_consistent_rewritten_plan_is_rejected_by_independent_binding(tmp_path, monkeypatch):
    async def run():
        from potluck_local.services import investigation_fixed_part_answers as fixed
        path, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', FixedRuntime)
        item = await create(scope)
        p = deepcopy(item['provenance'])
        plan = part_plans.prepare(QUESTION, ['Different access question?', 'What is the exact expiry date?'], origin='model_proposed')
        p['question_part_plan'] = plan
        plans = {cid: r['plan'] for cid, r in p['reviewed_unit_plan_receipts'].items() if r is not None}
        prepared = fixed.prepare(QUESTION, item['evidence'], QUESTION, question_part_plan=plan, unit_plans=plans)
        raw = p['reviewed_claim_selection']['raw']
        brief, trace = fixed.make_brief(QUESTION, p['operation_plan'][0], item['evidence'], p['snippet_catalog'], raw,
                                      question_part_plan=plan, unit_plans=plans)
        p['reviewed_claim_selection'] = trace
        p['reviewed_claim_selection_input'] = fixed.input_receipt(prepared)
        p['input_budget'] = {**p['input_budget'], **p['reviewed_claim_selection_input']}
        p['reviewed_claim_review_input'] = {**p['reviewed_claim_review_input'], **fixed.review_receipt(prepared, trace['selection'])}
        rewritten = {**item, 'brief': brief, 'provenance': p}
        fixed.validate_brief(rewritten, p['snippet_catalog'])  # Self-consistent hashes alone pass pure replay.
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET brief=?,provenance=? WHERE id=?', (json.dumps(brief), json.dumps(p), item['id']))
        with pytest.raises(ValueError, match='Question-part contract changed'):
            await inv.get_investigation(scope, item['id'])
    asyncio.run(run())


def test_contract_write_failure_rolls_back_all_artifacts_and_schema(tmp_path, monkeypatch):
    async def run():
        from potluck_local.services import investigation_question_part_contracts as contracts
        path, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        runtime = FixedRuntime()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        persist = contracts.persist_in_transaction
        async def fail(*args, **kwargs):
            await persist(*args, **kwargs)
            raise RuntimeError('Synthetic post-binding failure')
        monkeypatch.setattr(contracts, 'persist_in_transaction', fail)
        with pytest.raises(RuntimeError, match='post-binding'):
            await create(scope)
        assert counts(path) == (0, 0) and len(runtime.calls) == 2
        with sqlite3.connect(path) as db:
            columns = {r[1] for r in db.execute('PRAGMA table_info(investigations)')}
        assert not {'unit_plan_contract', 'question_part_contract'} & columns
    asyncio.run(run())


@pytest.mark.parametrize('stage', [1, 2])
def test_output_limit_never_retries_or_saves(tmp_path, monkeypatch, stage):
    async def run():
        path, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        class Limited(FixedRuntime):
            async def chat(self, *args, **kwargs):
                result = await super().chat(*args, **kwargs)
                if len(self.calls) == stage: result['choices'][0]['finish_reason'] = 'length'
                return result
        runtime = Limited()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        with pytest.raises(ValueError, match='output limit'): await create(scope)
        assert len(runtime.calls) == stage and counts(path) == (0, 0)
    asyncio.run(run())


def test_cancellation_recovery_requires_a_new_explicit_create(tmp_path, monkeypatch):
    async def run():
        path, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        entered = asyncio.Event()
        class Blocked(FixedRuntime):
            async def chat(self, *args, **kwargs):
                result = await super().chat(*args, **kwargs)
                if len(self.calls) == 2:
                    entered.set()
                    await asyncio.Event().wait()
                return result
        runtime = Blocked()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        task = asyncio.create_task(create(scope))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert counts(path) == (0, 0) and len(runtime.calls) == 2
        replacement = FixedRuntime()
        monkeypatch.setattr(inv, 'get_runtime', lambda: replacement)
        saved = await create(scope)
        assert saved['status'] == 'draft' and counts(path)[0] == 1
        assert len(replacement.calls) == 2 and len(runtime.calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize('tamper', ['unknown_source', 'presentation', 'wrong_state'])
def test_saved_correction_tampering_blocks_reopen_and_export(tmp_path, monkeypatch, tamper):
    async def run():
        path, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', FixedRuntime)
        item = await create(scope)
        corrected = deepcopy(item['brief'])
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'fixture reviewer', 'Synthetic correction', corrected_brief=corrected)
        review = deepcopy(reviewed['review'])
        if tamper == 'unknown_source': review['corrected_brief']['answers'][0]['snippet_ids'] = ['S999']
        elif tamper == 'presentation': review['corrected_brief']['answers'][0]['passage_presentation'] = deepcopy(item['brief']['answers'][0]['passage_presentation'])
        else: review['decision'] = 'accept'
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET review=? WHERE id=?', (json.dumps(review), item['id']))
        with pytest.raises(ValueError): await inv.get_investigation(scope, item['id'])
        from fastapi import HTTPException
        with pytest.raises(HTTPException): await routes.export(item['id'], scope.workspace, list(scope.collection_ids))
    asyncio.run(run())


@pytest.mark.parametrize('stage', [1, 2])
@pytest.mark.parametrize('fault', ['timeout', 'model_change'])
def test_runtime_failure_has_no_save_or_hidden_retry(tmp_path, monkeypatch, stage, fault):
    async def run():
        path, scope, _, _, _ = await setup(tmp_path, monkeypatch)
        class Failed(FixedRuntime):
            changed = False
            def current_model_sha256(self): return ('b' if self.changed else 'a') * 64
            async def chat(self, *args, **kwargs):
                result = await super().chat(*args, **kwargs)
                if len(self.calls) == stage:
                    if fault == 'timeout': raise asyncio.TimeoutError('Synthetic timeout')
                    self.changed = True
                return result
        runtime = Failed()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        with pytest.raises(ValueError): await create(scope)
        assert len(runtime.calls) == stage and counts(path) == (0, 0)
    asyncio.run(run())


def test_absent_unit_plan_is_frozen_until_new_approval(tmp_path, monkeypatch):
    async def run():
        path, scope, _, _, proposal = await setup(tmp_path, monkeypatch, approve_plan=False)
        class Whole(FixedRuntime):
            async def chat(self, *args, **kwargs):
                result = await super().chat(*args, **kwargs)
                result['choices'][0]['message']['content'] = result['choices'][0]['message']['content'].replace('C1:rule', 'C1:whole')
                return result
        monkeypatch.setattr(inv, 'get_runtime', Whole)
        item = await create(scope)
        assert set(item['provenance']['reviewed_unit_plan_receipts'].values()) == {None}
        await unit_plans.approve_plan('w', scope.collection_ids, proposal, 'fixture reviewer', 'Synthetic new approval')
        with pytest.raises(LookupError): await inv.get_investigation(scope, item['id'])
    asyncio.run(run())
