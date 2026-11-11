"""Real service/SQLite request-wide context checks; fake runtime only."""
import asyncio
from copy import deepcopy
import json
import sqlite3

import pytest
from fastapi import HTTPException
import test_investigation_fixed_part_answers_service as shared
from potluck_local.services import investigations as inv


class ContextRuntime(shared.FixedRuntime):
    async def chat(self, messages, **kwargs):
        reply = await super().chat(messages, **kwargs)
        if len(self.calls) == 1:
            payload = json.loads(messages[1]['content'])
            reply['choices'][0]['message']['content'] = json.dumps({
                'inventory': [[] for _ in payload['inventory_unit_order']]})
        if len(self.calls) == 2:
            reply['choices'][0]['message']['content'] = json.dumps({
                'parts': {'P1': {'check':'Approval stated.','answer':['C1:rule'],'alternative':[],'context':[]},
                          'P2': {'check':'Expiry absent.','answer':[],'alternative':[],'context':[]}}, 'context': ['C1:other']})
        return reply


@pytest.fixture(autouse=True)
def mode(monkeypatch):
    monkeypatch.setattr(shared, 'MODE', 'fixed-part-selection-v17')


@pytest.mark.parametrize('decision', ['accept', 'correct'])
def test_context_persists_with_source_citation_and_unknown(tmp_path, monkeypatch, decision):
    async def run():
        _, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        runtime = ContextRuntime(); monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        item = await shared.create(scope); original = deepcopy(item)
        answer = item['brief']['answers'][0]; presentation = answer['passage_presentation']
        context = presentation['request_context']
        assert answer['status'] == 'unknown' and presentation['parts'][1]['verdict'] == 'unknown'
        assert context['visible_unit_ids'] == ['C1:other']
        assert 'Request-wide context: [4]' in answer['text']
        assert answer['text'].count('Unrelated retention is 30 days.') == 1
        assert r'\nUnrelated retention' not in answer['text']
        passage = next(p for p in presentation['passages'] if p['unit_id'] == 'C1:other')
        assert passage['snippet_ids'] and set(passage['snippet_ids']) <= set(answer['snippet_ids'])
        assert any('Unrelated retention is 30 days.' in x['quote'] for x in answer['source_excerpts'])
        assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
        corrected = deepcopy(item['brief']) if decision == 'correct' else None
        await inv.review_investigation(scope, item['id'], decision, 'reviewer', 'Fixture review', corrected_brief=corrected)
        reopened = await inv.get_investigation(scope, item['id'])
        assert reopened['brief'] == original['brief'] and reopened['provenance'] == original['provenance']
        exported = (await shared.routes.export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        assert 'Unrelated retention is 30 days' in exported
        assert len(runtime.calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize('target', ['context_id', 'context_text', 'omit_context', 'trace'])
def test_context_tamper_blocks_get_and_export(tmp_path, monkeypatch, target):
    async def run():
        path, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', ContextRuntime)
        item = await shared.create(scope)
        brief, provenance = deepcopy(item['brief']), deepcopy(item['provenance'])
        ctx = brief['answers'][0]['passage_presentation']['request_context']
        if target == 'context_id': ctx['visible_unit_ids'] = ['C1:foreign']
        elif target == 'context_text': ctx['passages'][0]['text'] = 'Forged.'
        elif target == 'omit_context':
            before_text = brief['answers'][0]['text']
            brief['answers'][0]['text'] = before_text.replace('Unrelated retention is 30 days.', '')
            assert brief['answers'][0]['text'] != before_text
        else: provenance['reviewed_claim_selection']['review']['request_context']['requested_unit_ids'] = []
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET brief=?,provenance=? WHERE id=?', (json.dumps(brief), json.dumps(provenance), item['id']))
        with pytest.raises(ValueError): await inv.get_investigation(scope, item['id'])
        with pytest.raises(HTTPException): await shared.routes.export(item['id'], scope.workspace, list(scope.collection_ids))
    asyncio.run(run())
