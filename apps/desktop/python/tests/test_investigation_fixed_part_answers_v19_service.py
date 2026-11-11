"""Exercise the same persistence adversaries under the separate evidence-role mode."""
import asyncio
from copy import deepcopy
import json
import sqlite3

import pytest

import test_investigation_fixed_part_answers_service as shared
from potluck_local.services import investigations as inv
from potluck_local.services import investigation_question_part_contracts as contracts


class GroupRuntime(shared.FixedRuntime):
    async def chat(self, messages, **kwargs):
        reply = await super().chat(messages, **kwargs)
        if len(self.calls) == 1 and self.failure != 1:
            payload = json.loads(messages[1]['content'])
            reply['choices'][0]['message']['content'] = json.dumps({
                'inventory': [['evidence','none'] if uid == 'C1:rule' else ['none','none'] for uid in payload['inventory_unit_order']], 'context':[False for _ in payload['inventory_unit_order']]})
        if len(self.calls) == 2 and self.failure != 2:
            reply['choices'][0]['message']['content'] = json.dumps({
                'parts': {'P1': {'check':'UNTRUSTED_CHECK_ONLY: approval stated.','verdict':'supported','evidence':['C1:rule'],'context':[]},
                          'P2': {'check':'UNTRUSTED_CHECK_ONLY: expiry absent.','verdict':'unknown','evidence':[],'context':[]}}, 'context': []})
        return reply


@pytest.fixture(autouse=True)
def group_mode(monkeypatch):
    monkeypatch.setattr(shared, 'MODE', 'fixed-part-selection-v19')
    monkeypatch.setattr(shared, 'FixedRuntime', GroupRuntime)


# Reusing the contract suite keeps races, cancellation and tamper requirements equal.
for _name in dir(shared):
    if (_name.startswith('test_')
            and _name != 'test_self_consistent_rewritten_plan_is_rejected_by_independent_binding'):
        globals()[_name] = getattr(shared, _name)


def test_self_consistent_rewritten_plan_is_rejected_by_independent_binding(tmp_path, monkeypatch):
    async def run():
        from potluck_local.services import investigation_fixed_part_answers_v19 as fixed
        path, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', GroupRuntime)
        item = await shared.create(scope)
        p = deepcopy(item['provenance'])
        plan = shared.part_plans.prepare(shared.QUESTION,
            ['Different access question?', 'What is the exact expiry date?'], origin='model_proposed')
        p['question_part_plan'] = plan
        plans = {cid: r['plan'] for cid, r in p['reviewed_unit_plan_receipts'].items() if r is not None}
        prepared = fixed.prepare(shared.QUESTION, item['evidence'], shared.QUESTION,
                                 question_part_plan=plan, unit_plans=plans)
        brief, trace = fixed.make_brief(shared.QUESTION, p['operation_plan'][0], item['evidence'],
            p['snippet_catalog'], p['reviewed_claim_selection']['raw'],
            question_part_plan=plan, unit_plans=plans)
        p['reviewed_claim_selection'] = trace
        p['reviewed_claim_selection_input'] = fixed.input_receipt(prepared)
        p['input_budget'] = {**p['input_budget'], **p['reviewed_claim_selection_input']}
        p['reviewed_claim_review_input'] = {
            **p['reviewed_claim_review_input'], **fixed.review_receipt(prepared, trace['selection'])}
        fixed.validate_brief({**item, 'brief': brief, 'provenance': p}, p['snippet_catalog'])
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET brief=?,provenance=? WHERE id=?',
                       (json.dumps(brief), json.dumps(p), item['id']))
        with pytest.raises(ValueError, match='Question-part contract changed'):
            await inv.get_investigation(scope, item['id'])
    asyncio.run(run())


@pytest.mark.parametrize('older', ['fixed-part-selection-v1', 'fixed-part-selection-v2', 'fixed-part-selection-v3', 'fixed-part-selection-v4', 'fixed-part-selection-v5', 'fixed-part-selection-v6', 'fixed-part-selection-v7', 'fixed-part-selection-v8', 'fixed-part-selection-v9', 'fixed-part-selection-v10', 'fixed-part-selection-v11', 'fixed-part-selection-v12', 'fixed-part-selection-v13', 'fixed-part-selection-v14', 'fixed-part-selection-v15', 'fixed-part-selection-v16', 'fixed-part-selection-v17', 'fixed-part-selection-v18'])
def test_displayed_answer_preserves_part_status_and_saved_binding(tmp_path, monkeypatch, older):
    async def run():
        path, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', GroupRuntime)
        original = await shared.create(scope)
        answer = original['brief']['answers'][0]
        assert answer['passage_presentation']['version'] == 'fixed-part-selection-v19'
        assert answer['passage_presentation']['answer'] == answer['text']
        assert 'P1: What is required for access?\nEvidence:' in answer['text']
        assert '— Supported' not in answer['text']
        assert 'P2: What is the exact expiry date? — Not established by the selected reviewed statements.' in answer['text']
        assert answer['text'].count('Approval is required.') == 1
        with sqlite3.connect(path) as db:
            unit, part = db.execute('SELECT unit_plan_contract, question_part_contract '
                                    'FROM investigations WHERE id=?', (original['id'],)).fetchone()
            assert unit == 'fixed-part-selection-v19'
            assert json.loads(part)['version'] == 'fixed-part-selection-v19'
            p = deepcopy(original['provenance'])
            p['presentation_mode'] = older
            db.execute('UPDATE investigations SET provenance=? WHERE id=?',
                       (json.dumps(p), original['id']))
        with pytest.raises(ValueError):
            await inv.get_investigation(scope, original['id'])
    asyncio.run(run())


@pytest.mark.parametrize('wrong', ['rebind', 'workspace', 'scope'])
def test_binding_refusal_changes_no_draft_or_scope(tmp_path, monkeypatch, wrong):
    async def run():
        import aiosqlite
        from potluck_local.services.evidence_lookup import EvidenceScope
        path, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', GroupRuntime)
        original = await shared.create(scope)
        before = await inv.get_investigation(scope, original['id'])
        attempted = (scope if wrong == 'rebind' else
                     EvidenceScope('other', scope.collection_ids) if wrong == 'workspace' else
                     EvidenceScope(scope.workspace, ('other',)))
        async with aiosqlite.connect(path) as db:
            await db.execute('BEGIN IMMEDIATE')
            if wrong != 'rebind':
                await db.execute('UPDATE investigations SET question_part_contract=NULL WHERE id=?',
                                 (original['id'],))
            cursor = await db.execute('SELECT * FROM investigations WHERE id=?', (original['id'],))
            snapshot = await cursor.fetchone()
            with pytest.raises(ValueError, match='binding failed'):
                await contracts.persist_in_transaction(db, attempted, original['id'],
                                                        shared.make_plan(), version=shared.MODE)
            cursor = await db.execute('SELECT * FROM investigations WHERE id=?', (original['id'],))
            assert await cursor.fetchone() == snapshot
            await db.rollback()
        assert await inv.get_investigation(scope, original['id']) == before
    asyncio.run(run())


@pytest.mark.parametrize('decision', ['accept', 'correct'])
def test_untrusted_check_stays_in_provenance_not_answers_or_exports(tmp_path, monkeypatch, decision):
    async def run():
        _, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', GroupRuntime)
        item = await shared.create(scope)
        marker = 'UNTRUSTED_CHECK_ONLY'
        assert marker in json.dumps(item['provenance']['reviewed_claim_selection']['review']['parts'])
        assert marker not in json.dumps(item['brief'])
        export = await shared.routes.export(item['id'], scope.workspace, list(scope.collection_ids))
        assert marker not in export['markdown']
        reviewed = await inv.review_investigation(scope, item['id'], decision, 'fixture reviewer',
            'Synthetic boundary check', corrected_brief=deepcopy(item['brief']) if decision == 'correct' else None)
        reopened = await inv.get_investigation(scope, item['id'])
        assert reopened['provenance'] == item['provenance']
        assert marker not in json.dumps(reopened['brief'])
        assert marker not in (await shared.routes.export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        await inv.record_outcome(scope, item['id'], 'fixture reviewer', 'inconclusive', 'Synthetic result')
    asyncio.run(run())


@pytest.mark.parametrize('target', ['presentation', 'trace'])
def test_check_tamper_blocks_reopen_and_export(tmp_path, monkeypatch, target):
    async def run():
        from fastapi import HTTPException
        path, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', GroupRuntime)
        item = await shared.create(scope)
        brief, provenance = deepcopy(item['brief']), deepcopy(item['provenance'])
        if target == 'presentation':
            brief['answers'][0]['passage_presentation']['parts'][0]['evidence_check'] = 'untrusted display injection'
        else:
            provenance['reviewed_claim_selection']['review']['parts'][0]['evidence_check'] = 'forged check'
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET brief=?,provenance=? WHERE id=?',
                       (json.dumps(brief), json.dumps(provenance), item['id']))
        with pytest.raises(ValueError):
            await inv.get_investigation(scope, item['id'])
        with pytest.raises(HTTPException):
            await shared.routes.export(item['id'], scope.workspace, list(scope.collection_ids))
    asyncio.run(run())


def test_exact_budget_and_schema_receipts_then_accept_outcome(tmp_path, monkeypatch):
    async def run():
        path, scope, claim, plan, old = await shared.setup(tmp_path, monkeypatch)
        runtime = GroupRuntime()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        item = await shared.create(scope)
        assert item['status'] == 'draft' and item['review'] is None
        assert item['provenance']['question_part_plan'] == shared.make_plan()
        assert 'question_part_contract' not in item and 'unit_plan_contract' not in item
        assert item['brief']['answers'][0]['passage_presentation']['needs_review'] is True
        for stage, (measured, dispatched) in enumerate(zip(runtime.measurements, runtime.calls), 1):
            assert measured[0] == dispatched[0]
            assert measured[1]['response_format'] == dispatched[1]['response_format']
            schema = dispatched[1]['response_format']['schema']
            assert set(schema['properties']) == ({'inventory', 'context'} if stage == 1 else {'parts', 'context'})
            if stage == 2:
                assert set(schema['properties']['parts']['properties']) == {'P1', 'P2'}
                assert schema['properties']['parts']['properties']['P1']['properties']['check']['maxLength'] == 40
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
        replacement = await shared.unit_plans.propose_plan('w', scope.collection_ids, claim, plan)
        await shared.unit_plans.supersede_plan('w', scope.collection_ids, old, replacement, 'fixture reviewer', 'Synthetic replacement')
        with pytest.raises(LookupError):
            await inv.get_investigation(scope, item['id'])
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await shared.routes.export(item['id'], scope.workspace, list(scope.collection_ids))
        assert await inv.list_investigations(scope) == []
    asyncio.run(run())


def test_unknown_scope_basis_survives_save_read_and_export(tmp_path,monkeypatch):
    class BasisRuntime(GroupRuntime):
        async def chat(self,messages,**kwargs):
            reply=await super().chat(messages,**kwargs)
            if len(self.calls)==2:
                data=json.loads(reply['choices'][0]['message']['content'])
                data['parts']['P2']['evidence']=['C1:other']
                reply['choices'][0]['message']['content']=json.dumps(data)
            return reply
    async def run():
        _,scope,_,_,_=await shared.setup(tmp_path,monkeypatch)
        runtime=BasisRuntime();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
        item=await shared.create(scope);answer=item['brief']['answers'][0];row=answer['passage_presentation']['parts'][1]
        assert row['verdict']=='unknown' and row['evidence_unit_ids']==['C1:other'] and 'answer_unit_ids' not in row
        assert 'Scope basis:' in answer['text'] and 'Not established by the selected reviewed statements' in answer['text']
        assert (await inv.get_investigation(scope,item['id']))['brief']==item['brief']
        exported=await shared.routes.export(item['id'],scope.workspace,list(scope.collection_ids))
        assert 'Scope basis:' in exported['markdown'] and len(runtime.calls)==2
    asyncio.run(run())
