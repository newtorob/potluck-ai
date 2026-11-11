import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection_v13 as selection


def raw(ids):
    return json.dumps({'unit_ids': ids})

def reviewed(ids, status='supported', draft='Only approved rollout releases may proceed: use a signed build through chooseBuild, keep the rollback record, and wait while the incident remains open. The rollout client uses chooseBuild for display.'):
    return json.dumps({'review_unit_ids':ids, 'missing_start_token_id':'T6' if status=='unknown' else '',
        'missing_end_token_id':'T7' if status=='unknown' else '', 'conflicting':status=='conflicting', 'draft':draft})


@pytest.mark.parametrize('status', ['supported', 'conflicting', 'unknown'])
def test_real_service_source_render_get_review_export_and_revocation(tmp_path, monkeypatch, status):
    import asyncio
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.routers import investigations as routes
    from potluck_local.services import investigation_verification as verification
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))

    known = status != 'unknown'
    question = 'Which rollout build?' if known else 'Which rollout build and which exact time?'
    calls = []; measurements = []
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def validate_investigation_profile(self, *args, **kwargs): return None
        async def measure_investigation_prompt(self, messages, **kwargs):
            measurements.append(deepcopy((messages, kwargs)))
            return {'input_tokens': 123 + len(measurements), 'context_tokens': 8192}
        async def chat(self, messages, **kwargs):
            calls.append(deepcopy((messages, kwargs)))
            expected = ['unit_ids'] if len(calls)==1 else ['review_unit_ids','missing_start_token_id','missing_end_token_id','conflicting','draft']
            assert list(kwargs['response_format']['schema']['properties']) == expected
            reply = raw(['U2']) if len(calls)==1 else reviewed(['U2'],status)
            return {'choices': [{'message': {'content': reply}}]}

    monkeypatch.setattr(inv, 'get_runtime', Runtime)

    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'; folder.mkdir()
        body = 'Only approved rollout releases may proceed:\n- Use a signed build through the helper, `chooseBuild(x)`.\n- Keep the rollback record.\n- The rollout client uses `chooseBuild` for display.\n\nUnless the incident remains open, in which case wait.'
        source = folder / 'source.md'; source.write_text('# Rollout\n\n' + body)
        collection = await register_collection('w', 'docs', folder)
        await scan_collection('w', collection)
        scope = EvidenceScope('w', (collection,))
        found = await lookup_evidence(scope, 'rollout', 10, retrieval_version='v2')
        refs = [{k: found['source_chunks'][0][k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}]
        claim = await propose_claim('w', (collection,), 'rollout', body, refs)
        await approve_claim('w', (collection,), claim, 'test-reviewer', 'fixture source checked')
        item = await inv.create_investigation(scope, question, 'rollout', 'model',
            template_version='investigation-v21', reviewed_operations=[{'request': question, 'operation': 'answer'}],
            evidence_selection='reviewed-current-v1', presentation_mode=selection.VERSION,
            generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1',
            reliability_mode='bounded-evidence-v1')
        text = item['brief']['answers'][0]['text']
        assert all(value in text for value in ('Only approved', 'signed build', 'rollback record', 'incident remains open'))
        assert item['brief']['answers'][0]['status'] == status
        if not known:
            assert 'The selected statements do not specify the requested detail: “exact time?”' in text
        assert item['brief']['answers'][0]['passage_presentation']['version'] == 'reviewed-claim-selection-v13'
        assert 'unverified draft' in item['provenance']['factual_generation']
        assert [c[1]['max_tokens'] for c in calls] == [2000,512]
        assert [c[1]['expected_context_tokens'] for c in calls] == [8192]*2
        assert [c[1]['expected_sha256'] for c in calls] == ['a'*64]*2
        assert len(measurements)==2
        assert all(m[0] == c[0] for m, c in zip(measurements, calls))
        trace = item['provenance']['reviewed_claim_selection']
        review_input = item['provenance']['reviewed_claim_review_input']
        assert trace['version'] == selection.VERSION
        assert trace['renderer_version'] == 'reviewed-claim-selection-v6'
        assert len(trace['context_edges']) == 1
        assert trace['context_edges'][0]['identifier'] == 'chooseBuild'
        assert trace['context_edges'][0]['to'] in trace['selection']['proposal_context_unit_ids']
        assert 'rollout client' in text
        assert review_input['input_tokens'] == 125 and review_input['output_tokens'] == 512
        assert review_input['proposal_context_unit_ids'] == trace['selection']['proposal_context_unit_ids']
        assert 'reviewed_claim_scope_input' not in item['provenance']
        assert 'scope' not in trace
        assert set(json.loads(trace['raw'])) == {'selection_raw','review_raw'}
        assert trace['review']['missing_request_quote'] == ('' if known else 'exact time?')
        injected = deepcopy(item); injected['provenance']['reviewed_claim_scope_input'] = {}
        with pytest.raises(ValueError): selection.validate_brief(injected, item['provenance']['snippet_catalog'])
        for field, value in [('input_tokens', True), ('input_tokens', 8192), ('context_tokens', 4096), ('output_tokens', 2000), ('proposal_context_unit_ids', []), ('proposal_unit_ids', [])]:
            broken = deepcopy(item); broken['provenance']['reviewed_claim_review_input'][field] = value
            with pytest.raises(ValueError):
                selection.validate_brief(broken, item['provenance']['snippet_catalog'])
        assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
        # Reads and exports reject corrupted stored originals before presenting them.
        import aiosqlite
        from fastapi import HTTPException
        original_provenance = deepcopy(item['provenance'])
        for field in ('context_policy', 'context_edges', 'pipeline_input_sha256', 'proposal_policy', 'boundary_policy', 'draft_policy', 'review_draft', 'review_offsets', 'review_token_digest', 'review_missing_start_token_id', 'review_status', 'review_boundary_policy', 'review_tokenizer', 'raw', 'input_messages', 'presentation_mode', 'dropped_proposal_unit_ids', 'dropped_dependency_only_unit_ids'):
            broken = deepcopy(original_provenance)
            if field == 'input_messages':
                broken['reviewed_claim_selection_input']['messages_sha256'] = 'forged'
            elif field == 'presentation_mode':
                broken['presentation_mode'] = 'reviewed-claim-selection-v9'
            elif field.startswith('review_'):
                broken['reviewed_claim_selection']['review'][field[len('review_'):]] = 'forged'
            elif field in ('dropped_proposal_unit_ids', 'dropped_dependency_only_unit_ids'):
                broken['reviewed_claim_selection']['review'][field] = ['forged']
            elif field == 'context_edges':
                broken['reviewed_claim_selection'][field] = []
            else:
                broken['reviewed_claim_selection'][field] = 'forged'
            async with aiosqlite.connect(tmp_path / 'db.sqlite') as db:
                await db.execute('UPDATE investigations SET provenance=? WHERE id=?',
                                 (json.dumps(broken), item['id']))
                await db.commit()
            with pytest.raises(ValueError):
                await inv.get_investigation(scope, item['id'])
            with pytest.raises(HTTPException):
                await routes.export(item['id'], scope.workspace, list(scope.collection_ids))
        async with aiosqlite.connect(tmp_path / 'db.sqlite') as db:
            await db.execute('UPDATE investigations SET provenance=? WHERE id=?',
                             (json.dumps(original_provenance), item['id']))
            await db.execute('UPDATE investigations SET brief=? WHERE id=?',
                             (json.dumps({**item['brief'], 'answers': [{**item['brief']['answers'][0], 'text': 'forged'}]}), item['id']))
            await db.commit()
        with pytest.raises(ValueError):
            await inv.get_investigation(scope, item['id'])
        async with aiosqlite.connect(tmp_path / 'db.sqlite') as db:
            await db.execute('UPDATE investigations SET brief=? WHERE id=?', (json.dumps(item['brief']), item['id']))
            await db.commit()
        assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
        exported = (await routes.export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        assert 'Presented reviewed claim IDs: C1' in exported and 'incident remains open' in exported
        with pytest.raises(ValueError, match='Automatic verification is unsupported'):
            verification.original_input(item)
        forged = deepcopy(item['brief']); forged['answers'][0]['source_excerpts'][0]['quote'] += ' forged'
        with pytest.raises(inv.BriefValidationError):
            await inv.review_investigation(scope, item['id'], 'correct', 'test-reviewer', 'tamper test', corrected_brief=forged)
        assert (await inv.get_investigation(scope, item['id']))['review'] is None
        corrected = deepcopy(item['brief'])
        corrected['answers'][0]['text'] = 'Use a signed build after approval; wait while the incident remains open.' + ('' if known else ' Exact time is unstated in the selected statements.')
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'test-reviewer', 'fixture correction', corrected_brief=corrected)
        assert reviewed['brief'] == item['brief']
        reread = await inv.get_investigation(scope, item['id'])
        assert reread['review']['corrected_brief']['answers'][0]['text'] == corrected['answers'][0]['text']
        assert 'passage_presentation' not in reread['review']['corrected_brief']['answers'][0]
        exported = (await routes.export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        assert 'The sections above use the reviewed correction. The original draft is retained in Potluck.' in exported
        source.write_text('# Rollout\n\nA changed source.'); await scan_collection('w', collection)
        with pytest.raises((ValueError, LookupError)):
            await inv.get_investigation(scope, item['id'])
    asyncio.run(run())


@pytest.mark.parametrize('keep_oversized_final', [False, True])
def test_real_service_oversized_proposal_is_not_a_saved_answer(tmp_path, monkeypatch, keep_oversized_final):
    import asyncio
    import aiosqlite
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim
    from potluck_local.services.source_collections import register_collection, scan_collection
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    calls = []; measured = []; selected = {}
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def validate_investigation_profile(self, *args, **kwargs): return None
        async def measure_investigation_prompt(self, messages, **kwargs):
            measured.append(deepcopy(messages))
            return {'input_tokens': 500, 'context_tokens': 8192}
        async def chat(self, messages, **kwargs):
            calls.append(deepcopy(messages)); payload = json.loads(messages[1]['content'])
            units = [u for claim in payload['claims'] for u in claim['units']]
            if len(calls) == 1:
                selected['proposal'] = next(u['id'] for u in units if 'helper `mix(x)`' in u['text'])
                selected['final'] = next(u['id'] for u in units if 'Verify signature.' in u['text'])
                reply = raw([selected['proposal']])
            else:
                assert len(payload['proposal_context_unit_ids']) > 16
                assert payload['proposal_unit_ids'] == [selected['proposal']]
                assert 'retained_unit_ids' not in payload
                reply = reviewed([selected['proposal'] if keep_oversized_final else selected['final']], draft='Only approved builds proceed: Verify signature. Keep rollback record. Wait while the incident is open.')
            return {'choices': [{'message': {'content': reply}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db(); folder = tmp_path / 'docs'; folder.mkdir()
        body = ('## Background\n- A helper `mix(x)` acts.\n' +
                '\n'.join('- UI uses `mix` for item '+str(i)+'.' for i in range(17)) +
                '\n\n## Rollout\nOnly approved builds proceed:\n- Verify signature.\n- Keep rollback record.\n\nUnless an incident is open, in which case wait.')
        (folder / 'source.md').write_text('# Rollout\n\n' + body)
        collection = await register_collection('w', 'docs', folder); await scan_collection('w', collection)
        scope = EvidenceScope('w', (collection,)); found = await lookup_evidence(scope, 'rollout', 10, retrieval_version='v2')
        refs = [{k: chunk[k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')} for chunk in found['source_chunks']]
        claim = await propose_claim('w', (collection,), 'rollout', body, refs)
        await approve_claim('w', (collection,), claim, 'fixture', 'source checked')
        question = 'What must the approved rollout do and when must it wait?'
        async def create():
            return await inv.create_investigation(scope, question, 'rollout', 'model',
                template_version='investigation-v21', reviewed_operations=[{'request': question, 'operation': 'answer'}],
                evidence_selection='reviewed-current-v1', presentation_mode=selection.VERSION,
                generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1',
                reliability_mode='bounded-evidence-v1')
        if keep_oversized_final:
            with pytest.raises(ValueError, match='context limit'):
                await create()
        else:
            item = await create(); text = item['brief']['answers'][0]['text']
            assert all(t in text for t in ('Only approved', 'Verify signature', 'Keep rollback', 'incident is open'))
            assert 'mix' not in text and 'Background' not in text
            trace = item['provenance']['reviewed_claim_selection']
            assert len(trace['selection']['proposal_context_unit_ids']) > 16
            assert len(trace['review']['unit_ids']) <= 16
            assert trace['review']['dropped_proposal_unit_ids'] == [selected['proposal']]
            assert trace['review']['dropped_dependency_only_unit_ids']
            assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
        assert len(calls) == len(measured) == 2
        async with aiosqlite.connect(tmp_path / 'db.sqlite') as db:
            assert (await (await db.execute('SELECT COUNT(*) FROM investigations')).fetchone())[0] == (0 if keep_oversized_final else 1)
    asyncio.run(run())




@pytest.mark.parametrize('failure,expected_calls,expected_measurements', [
    ('truncated_first',1,1), ('truncated_review',2,2), ('empty_draft',2,2), ('missing_draft',2,2), ('long_draft',2,2), ('nonstring_draft',2,2), ('unbound_draft',2,2),
    ('request_token_overflow',0,0), ('invalid_first',1,1), ('review_input_overflow',1,2), ('changed_capacity',1,2),
    ('invalid_review_unit',2,2), ('unpaired_boundary',2,2), ('foreign_boundary',2,2),
    ('reversed_boundary',2,2), ('wrong_boolean',2,2), ('conflict_with_missing',2,2),
    ('extra_key',2,2), ('duplicate_key',2,2), ('unexpected_status_field',2,2),
    ('long_boundary',2,2), ('source_change',2,2), ('changed_model',2,2),
    ('deadline_before_review',1,1), ('deadline_during_review',2,2),
    ('review_measure_timeout',1,2), ('persist_trace_rejection',2,2),
])
def test_two_call_failures_never_retry_or_save(tmp_path, monkeypatch, failure, expected_calls, expected_measurements):
    import asyncio
    import aiosqlite
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim
    from potluck_local.services.source_collections import register_collection, scan_collection
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH',str(tmp_path/'db.sqlite'))
    monkeypatch.setattr(inv,'get_settings',lambda:SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True),memory=SimpleNamespace(enabled=True)))
    calls=[];measurements=[];altered=False;clock=[100.0]
    monkeypatch.setattr(inv,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    async def run():
        nonlocal altered
        await init_memory_db();folder=tmp_path/'docs';folder.mkdir();body='A signed build requires approval.'
        source=folder/'source.md';source.write_text('# Rollout\n\n'+body)
        collection=await register_collection('w','docs',folder);await scan_collection('w',collection);scope=EvidenceScope('w',(collection,))
        found=await lookup_evidence(scope,'rollout',10,retrieval_version='v2')
        refs=[{k:found['source_chunks'][0][k] for k in ('collection_id','document_id','revision_id','chunk_id')}]
        claim=await propose_claim('w',(collection,),'rollout',body,refs);await approve_claim('w',(collection,),claim,'fixture','source checked')
        class Runtime:
            def current_model_id(self):return 'changed' if altered else 'model'
            def current_model_sha256(self):return 'a'*64
            async def validate_investigation_profile(self,*args,**kwargs):return None
            async def measure_investigation_prompt(self,messages,**kwargs):
                measurements.append(messages)
                if len(measurements)==2:
                    if failure=='review_input_overflow':return {'input_tokens':7681,'context_tokens':8192}
                    if failure=='changed_capacity':return {'input_tokens':100,'context_tokens':4096}
                    if failure=='review_measure_timeout':raise asyncio.TimeoutError
                return {'input_tokens':100,'context_tokens':8192}
            async def chat(self,messages,**kwargs):
                nonlocal altered
                calls.append(messages);assert len(calls)<=2
                if len(calls)==1:
                    if failure=='deadline_before_review':clock[0]+=241
                    reply='{"unit_ids":["missing"]}' if failure=='invalid_first' else raw(['U1'])
                else:
                    if failure=='source_change':
                        source.write_text('# Rollout\n\nChanged policy.');await scan_collection('w',collection)
                    if failure=='changed_model':altered=True
                    if failure=='deadline_during_review':raise asyncio.TimeoutError
                    if failure=='persist_trace_rejection':
                        def reject(*a,**k):raise ValueError('Injected invalid original trace')
                        monkeypatch.setattr(selection,'validate_brief',reject)
                    data=json.loads(reviewed(['U1'],draft='A signed build requires approval.'))
                    if failure=='empty_draft':data['draft']=''
                    if failure=='missing_draft':data.pop('draft')
                    if failure=='long_draft':data['draft']='x'*1801
                    if failure=='nonstring_draft':data['draft']=123
                    if failure=='unbound_draft':data.update(review_unit_ids=[],missing_start_token_id='T6',missing_end_token_id='T7')
                    if failure=='invalid_review_unit':data['review_unit_ids']=['missing']
                    if failure=='unpaired_boundary':data['missing_start_token_id']='T6'
                    if failure=='foreign_boundary':data.update(missing_start_token_id='T999',missing_end_token_id='T999')
                    if failure=='reversed_boundary':data.update(missing_start_token_id='T7',missing_end_token_id='T6')
                    if failure=='wrong_boolean':data['conflicting']=1
                    if failure=='conflict_with_missing':data.update(missing_start_token_id='T6',missing_end_token_id='T7',conflicting=True)
                    if failure=='extra_key':data['scope_raw']='{}'
                    if failure=='unexpected_status_field':data['status']='supported'
                    if failure=='long_boundary':data.update(missing_start_token_id='T4',missing_end_token_id='T4')
                    reply=json.dumps(data)
                    if failure=='duplicate_key':reply=reply[:-1]+',"conflicting":false}'
                return {'choices':[{'message':{'content':reply},'finish_reason':'length' if (failure=='truncated_first' and len(calls)==1) or (failure=='truncated_review' and len(calls)==2) else 'stop'}]}
        monkeypatch.setattr(inv,'get_runtime',Runtime)
        q=(' '.join(['word']*129) if failure=='request_token_overflow' else 'Which build has '+'x'*401 if failure=='long_boundary' else 'Which rollout build and which exact time?')
        with pytest.raises((ValueError,asyncio.TimeoutError)):
            await inv.create_investigation(scope,q,'rollout','model',template_version='investigation-v21',
                reviewed_operations=[{'request':q,'operation':'answer'}],evidence_selection='reviewed-current-v1',presentation_mode=selection.VERSION,
                generation_profile='qwen3.8-27b-native-non-thinking',prompt_encoding='compact-calculation-v1',reliability_mode='bounded-evidence-v1')
        assert len(calls)==expected_calls and len(measurements)==expected_measurements
        async with aiosqlite.connect(tmp_path/'db.sqlite') as db:
            assert (await (await db.execute('SELECT COUNT(*) FROM investigations')).fetchone())[0]==0
    asyncio.run(run())
