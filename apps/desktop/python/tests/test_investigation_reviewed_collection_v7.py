"""Full selected input/replay coverage is separate from semantic correctness."""
import asyncio
from copy import deepcopy
import json
import aiosqlite
import pytest
from potluck_local.services import investigation_reviewed_collection_v7 as gate
from potluck_local.services import investigation_context, investigations as inv, pilot_setup_drafts
from potluck_local.services.memory_db import get_memory_db_path
from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim, delete_claim
from potluck_local.services.source_collections import scan_collection
from test_investigation_reviewed_evidence import setup_case  # noqa: F401

QUERY = 'A question with zero lexical overlap cobalt'

async def full_claim(scope, path, *, approve=True, edit=False):
    draft=(await pilot_setup_drafts.document_draft(scope,scope.collection_ids[0],path))['preview']
    refs=[]
    for citation in draft['citation_ids']:
        _,chunk,_=citation.split(':')
        doc=next(s for d in draft['documents'] for s in d['opening']+d['excerpts'] if s['chunk_id']==chunk)
        refs.append({k:doc[k] for k in ('collection_id','document_id','revision_id','chunk_id')})
    cid=await propose_claim(scope.workspace,scope.collection_ids,path,draft['content']+(' Edited.' if edit else ''),refs)
    if approve: await approve_claim(scope.workspace,scope.collection_ids,cid,'ASSISTANT synthetic test','Complete source test, not owner acceptance.')
    return cid

async def prepared(setup):
    scope,folder,_=await setup();await full_claim(scope,'old.md');await full_claim(scope,'new.md')
    return scope,folder,await gate.prepare(scope,QUERY)

def item_for(receipt):
    return {'id':'test-record','evidence':deepcopy(receipt['result']['reviewed_claims']),'provenance':{'retrieval_query':QUERY,'evidence_selection':{'version':gate.VERSION,'snapshot_sha256':receipt['snapshot_sha256']},'document_coverage':deepcopy(receipt['result']['retrieval_trace']['document_coverage'])}}

async def bind(receipt):
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await gate.persist_in_transaction(db,'test-record',receipt);await db.commit()

async def validate(scope,item):
    async with aiosqlite.connect(get_memory_db_path()) as db:await gate.validate_in_transaction(db,scope,item)


def test_zero_overlap_includes_all_complete_reviewed_documents(setup_case):
    async def run():
        scope,_,r=await prepared(setup_case);result=r['result'];assert len(result['reviewed_claims'])==2
        assert {s['relative_path'] for c in result['reviewed_claims'] for s in c['sources']}=={'old.md','new.md'}
        c=result['retrieval_trace']['document_coverage'];assert c['selected_documents']==c['supplied_documents']==2 and c['omitted_documents']==0
        assert not result['truncated'] and not result['source_chunks']
        await bind(r);await validate(scope,item_for(r))
        async with aiosqlite.connect(get_memory_db_path()) as db:
            row=await (await db.execute('SELECT receipt FROM investigation_document_coverage')).fetchone()
            assert set(json.loads(row[0]))=={'binding_sha256'} and 'Rollout' not in row[0]
    asyncio.run(run())

@pytest.mark.parametrize('bad',['unapproved','edited','missing','empty'])
def test_incomplete_input_refuses_before_inference(setup_case,monkeypatch,bad):
    async def run():
        scope,folder,_=await setup_case();await full_claim(scope,'old.md')
        if bad not in ('missing','empty'):await full_claim(scope,'new.md',approve=bad!='unapproved',edit=bad=='edited')
        if bad=='empty':(folder/'new.md').write_text('');await scan_collection('w',scope.collection_ids[0])
        class Runtime:
            calls=0
            def current_model_id(self):return 'model'
            def current_model_sha256(self):return 'a'*64
            async def chat(self,*a,**kw):self.calls+=1;raise AssertionError('Must refuse before inference')
        runtime=Runtime();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        with pytest.raises(ValueError,match='selected document|Selected document'):
            await inv.create_investigation(scope,'What is documented?','What is documented?','model',template_version='investigation-v21',evidence_selection=gate.VERSION)
        assert runtime.calls==0
    asyncio.run(run())

@pytest.mark.parametrize('change',['add','edit','delete','symlink','failed-scan','stale-scan','unapprove'])
def test_live_inventory_and_authority_changes_refuse_replay(setup_case,change):
    async def run():
        scope,folder,r=await prepared(setup_case);await bind(r);item=item_for(r)
        if change=='add':(folder/'added.md').write_text('New answer exists.')
        elif change=='edit':(folder/'new.md').write_text('Updated answer.')
        elif change=='delete':(folder/'new.md').unlink()
        elif change=='symlink':(folder/'new.md').unlink();(folder/'new.md').symlink_to(folder/'old.md')
        elif change=='unapprove':await delete_claim('w',scope.collection_ids,r['result']['reviewed_claims'][0]['id'])
        else:
            async with aiosqlite.connect(get_memory_db_path()) as db:
                await db.execute("UPDATE source_collections SET "+("scan_status='failed'" if change=='failed-scan' else "last_scan_at='2020-01-01T00:00:00+00:00'"));await db.commit()
        with pytest.raises(ValueError):await gate.prepare(scope,QUERY)
        with pytest.raises(ValueError):await validate(scope,item)
    asyncio.run(run())


def test_excludes_and_unchanged_rescan_keep_same_logical_evidence(setup_case):
    async def run():
        scope,folder,r=await prepared(setup_case);await bind(r);item=item_for(r)
        (folder/'archive').mkdir();(folder/'archive/ignored.md').write_text('Excluded.')
        await scan_collection('w',scope.collection_ids[0]);await validate(scope,item)
        assert (await gate.prepare(scope,QUERY))['snapshot_sha256']==r['snapshot_sha256']
    asyncio.run(run())

@pytest.mark.parametrize('limit',['MAX_SELECTED','MAX_RECORD_CHARS','MAX_CLAIM_CHARS','MAX_TOTAL_CHARS','MAX_SNIPPETS'])
def test_input_budgets_refuse_whole_set_not_prefix(setup_case,monkeypatch,limit):
    async def run():
        scope,_,_=await prepared(setup_case);monkeypatch.setattr(investigation_context if limit=='MAX_SNIPPETS' else gate,limit,1)
        with pytest.raises(ValueError,match='limit'):await gate.prepare(scope,QUERY)
    asyncio.run(run())

@pytest.mark.parametrize('tamper',['digest','inventory','evidence','mode','missing-receipt','receipt','presented'])
def test_receipt_cannot_be_stripped_narrowed_or_relabelled(setup_case,tamper):
    async def run():
        scope,_,r=await prepared(setup_case);await bind(r);item=item_for(r)
        if tamper=='digest':item['provenance']['evidence_selection']['snapshot_sha256']='0'*64
        elif tamper=='inventory':item['provenance']['document_coverage']['documents'].pop()
        elif tamper=='evidence':item['evidence'].pop()
        elif tamper=='mode':item['provenance']={'evidence_selection':{'version':'reviewed-collection-v6'}}
        elif tamper=='presented':item['evidence'][0]['presented_content']='Clipped.'
        else:
            async with aiosqlite.connect(get_memory_db_path()) as db:
                if tamper=='missing-receipt':await db.execute('DELETE FROM investigation_document_coverage')
                else:await db.execute("UPDATE investigation_document_coverage SET receipt='{}'")
                await db.commit()
        with pytest.raises(ValueError):await validate(scope,item)
    asyncio.run(run())


def test_legacy_v6_without_contract_unchanged(setup_case):
    async def run():
        scope,_,_=await setup_case();await validate(scope,{'id':'old-record','provenance':{'evidence_selection':{'version':'reviewed-collection-v6'}}})
    asyncio.run(run())


def test_complete_source_setup_lists_zero_overlap_documents_without_approving(setup_case):
    async def run():
        scope,_,_=await setup_case()
        drafts=await pilot_setup_drafts.source_drafts(scope,QUERY,suggestion_version='all-selected-documents-v1')
        assert {d['relative_path'] for d in drafts['candidates']}=={'old.md','new.md'}
        assert not drafts['truncated'] and all(d['preview'] for d in drafts['candidates'])
        with pytest.raises(ValueError,match='approved'):await gate.prepare(scope,QUERY)
    asyncio.run(run())


def test_changed_index_cannot_appear_complete_against_unchanged_file(setup_case):
    async def run():
        scope,_,r=await prepared(setup_case);await bind(r)
        async with aiosqlite.connect(get_memory_db_path()) as db:
            await db.execute("UPDATE source_chunks SET content='Tampered source'");await db.commit()
        with pytest.raises(ValueError,match='coverage is no longer current'):await validate(scope,item_for(r))
    asyncio.run(run())


@pytest.mark.parametrize('rescan_during_generation', [False, True])
def test_real_persistence_and_routes_reject_new_unexamined_document(setup_case,monkeypatch,rescan_during_generation):
    from test_investigation_fixed_part_answers_service import FixedRuntime
    from potluck_local.services import investigation_request_parts as parts
    from potluck_local.routers import investigations as routes
    from fastapi import HTTPException
    async def run():
        scope,folder,_=await prepared(setup_case);q='What is documented?'
        class Runtime(FixedRuntime):
            async def chat(self,messages,**kwargs):
                self.calls.append(deepcopy((messages,kwargs)))
                payload=json.loads(messages[1]['content'])
                if len(self.calls)==1:
                    if rescan_during_generation:await scan_collection('w',scope.collection_ids[0])
                    raw=json.dumps({'parts':{'P1':[q]}})
                else:raw=json.dumps({'F1':{'decision':'established','sources':[s['id'] for s in payload['sources']]}})
                return {'choices':[{'message':{'content':raw},'finish_reason':'stop'}]}
        runtime=Runtime();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        item=await inv.create_investigation(scope,q,q,'model',template_version='investigation-v21',reviewed_operations=[{'request':q,'operation':'answer'}],evidence_selection=gate.VERSION,presentation_mode='fixed-part-selection-v43',generation_profile='qwen3.8-27b-native-non-thinking',prompt_encoding='compact-calculation-v1',reliability_mode='bounded-evidence-v1',question_part_plan=parts.prepare(q,[q],origin='fixture_supplied',request=q))
        assert len(runtime.calls)==3 and len(item['evidence'])==2
        assert all('Rollout new' in json.dumps(call[0]) and 'Rollout old' in json.dumps(call[0]) for call in runtime.calls[1:])
        reopened=await inv.get_investigation(scope,item['id']);assert reopened['brief']==item['brief']
        exported=await routes.export(item['id'],scope.workspace,list(scope.collection_ids));assert '2 of 2 selected indexed documents' in exported['markdown']
        await inv.review_investigation(scope,item['id'],'accept','ASSISTANT synthetic test','Synthetic route check only')
        (folder/'added.md').write_text('A previously unexamined answer.')
        for op in [lambda:inv.get_investigation(scope,item['id']),lambda:routes.export(item['id'],scope.workspace,list(scope.collection_ids)),lambda:inv.review_investigation(scope,item['id'],'accept','test','Stale must refuse')]:
            with pytest.raises((ValueError,LookupError,HTTPException)) as exc:await op()
            assert 'coverage' in str(exc.value).lower() or 'unavailable' in str(exc.value).lower()
        assert len(runtime.calls)==3
    asyncio.run(run())


def test_complete_input_context_budget_refuses_before_first_model_call(setup_case,monkeypatch):
    from test_investigation_fixed_part_answers_service import FixedRuntime
    from potluck_local.services import investigation_request_parts as parts
    async def run():
        scope,_,_=await prepared(setup_case);q='What is documented?'
        runtime=FixedRuntime(overflow=1);monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        with pytest.raises(ValueError):
            await inv.create_investigation(scope,q,q,'model',template_version='investigation-v21',reviewed_operations=[{'request':q,'operation':'answer'}],evidence_selection=gate.VERSION,presentation_mode='fixed-part-selection-v43',generation_profile='qwen3.8-27b-native-non-thinking',prompt_encoding='compact-calculation-v1',reliability_mode='bounded-evidence-v1',question_part_plan=parts.prepare(q,[q],origin='fixture_supplied',request=q))
        assert not runtime.calls and len(runtime.measurements)==1
    asyncio.run(run())


def test_partial_claim_cannot_prove_complete_document(setup_case):
    async def run():
        scope,_,r=await prepared(setup_case)
        async with aiosqlite.connect(get_memory_db_path()) as db:
            await db.execute("UPDATE reviewed_claims SET content='Rollout policy only' WHERE id=?",(r['result']['reviewed_claims'][0]['id'],));await db.commit()
        with pytest.raises(ValueError,match='complete approved'):await gate.prepare(scope,QUERY)
    asyncio.run(run())
