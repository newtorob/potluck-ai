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
        if len(self.calls) == 2 and self.failure != 2:
            reply['choices'][0]['message']['content'] = json.dumps({
                'P1': {'answer_groups': [['C1:rule']], 'context_unit_ids': []},
                'P2': {'answer_groups': [], 'context_unit_ids': []}})
        return reply


@pytest.fixture(autouse=True)
def group_mode(monkeypatch):
    monkeypatch.setattr(shared, 'MODE', 'fixed-part-selection-v3')
    monkeypatch.setattr(shared, 'FixedRuntime', GroupRuntime)


# Reusing the contract suite keeps races, cancellation and tamper requirements equal.
for _name in dir(shared):
    if (_name.startswith('test_')
            and _name != 'test_self_consistent_rewritten_plan_is_rejected_by_independent_binding'):
        globals()[_name] = getattr(shared, _name)


def test_self_consistent_rewritten_plan_is_rejected_by_independent_binding(tmp_path, monkeypatch):
    async def run():
        from potluck_local.services import investigation_fixed_part_answers_v3 as fixed
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


def test_displayed_answer_preserves_part_status_and_saved_binding(tmp_path, monkeypatch):
    async def run():
        path, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', GroupRuntime)
        original = await shared.create(scope)
        answer = original['brief']['answers'][0]
        assert answer['passage_presentation']['version'] == 'fixed-part-selection-v3'
        assert answer['passage_presentation']['answer'] == answer['text']
        assert 'P1:' in answer['text'] and 'do not establish P2:' in answer['text']
        assert 'do not establish P1:' not in answer['text']
        with sqlite3.connect(path) as db:
            unit, part = db.execute('SELECT unit_plan_contract, question_part_contract '
                                    'FROM investigations WHERE id=?', (original['id'],)).fetchone()
            assert unit == 'fixed-part-selection-v3'
            assert json.loads(part)['version'] == 'fixed-part-selection-v3'
            p = deepcopy(original['provenance'])
            p['presentation_mode'] = 'fixed-part-selection-v1'
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
