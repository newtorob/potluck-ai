"""Inventory is replay-bound classification metadata, never enforced evidence."""
import asyncio
from copy import deepcopy
import json
import sqlite3

import pytest
from fastapi import HTTPException
import test_investigation_fixed_part_answers_service as shared
from test_investigation_fixed_part_answers_v16_service import GroupRuntime
from potluck_local.services import investigations as inv


class EmptyInventoryRuntime(GroupRuntime):
    async def chat(self, messages, **kwargs):
        reply = await super().chat(messages, **kwargs)
        if len(self.calls) == 1:
            payload = json.loads(messages[1]['content'])
            reply['choices'][0]['message']['content'] = json.dumps({
                'inventory': [[] for _ in payload['inventory_unit_order']]})
        return reply


@pytest.fixture(autouse=True)
def mode(monkeypatch):
    monkeypatch.setattr(shared, 'MODE', 'fixed-part-selection-v16')


def test_review_overrides_complete_omitting_inventory_without_fake_evidence(tmp_path, monkeypatch):
    async def run():
        _, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        runtime = EmptyInventoryRuntime()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        item = await shared.create(scope)
        provenance = item['provenance']
        selection = provenance['reviewed_claim_selection']['selection']
        assert selection['inventory'] == {'C1:heading': [], 'C1:rule': [], 'C1:exception': [], 'C1:other': []}
        assert 'requested_unit_ids' not in selection and 'context_unit_ids' not in selection
        receipt = provenance['reviewed_claim_review_input']
        assert receipt['inventory_unit_ids'] == list(selection['inventory'])
        assert len(receipt['applicability_inventory_sha256']) == 64
        assert not {'proposal_unit_ids', 'proposal_context_unit_ids'} & receipt.keys()
        payload = json.loads(runtime.calls[1][0][1]['content'])
        assert payload['applicability_inventory'] == selection['inventory']
        answer = item['brief']['answers'][0]
        assert answer['passage_presentation']['parts'][0]['verdict'] == 'supported'
        assert 'Approval is required.' in answer['text']
        assert 'Suspended members are excluded.' in answer['text']
        assert 'Unrelated retention is 30 days.' not in answer['text']
        assert len(runtime.calls) == 2
        assert (await inv.get_investigation(scope, item['id']))['provenance'] == provenance
    asyncio.run(run())


@pytest.mark.parametrize('target', ['inventory', 'receipt_ids', 'receipt_digest', 'legacy_alias'])
def test_inventory_or_measurement_tamper_blocks_reopen_review_and_export(tmp_path, monkeypatch, target):
    async def run():
        path, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        monkeypatch.setattr(inv, 'get_runtime', EmptyInventoryRuntime)
        item = await shared.create(scope)
        provenance = deepcopy(item['provenance'])
        receipt = provenance['reviewed_claim_review_input']
        if target == 'inventory':
            provenance['reviewed_claim_selection']['selection']['inventory']['C1:other'] = ['P1']
        elif target == 'receipt_ids':
            receipt['inventory_unit_ids'].pop()
        elif target == 'receipt_digest':
            receipt['applicability_inventory_sha256'] = '0' * 64
        else:
            receipt['proposal_unit_ids'] = ['C1:other']
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET provenance=? WHERE id=?',
                       (json.dumps(provenance), item['id']))
        with pytest.raises(ValueError):
            await inv.get_investigation(scope, item['id'])
        with pytest.raises(ValueError):
            await inv.review_investigation(scope, item['id'], 'accept', 'reviewer', 'Fixture')
        with pytest.raises(HTTPException):
            await shared.routes.export(item['id'], scope.workspace, list(scope.collection_ids))
    asyncio.run(run())
