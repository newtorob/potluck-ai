"""Primary implementation of the actual V133 local-agent C1 proposal; no inference."""
import asyncio
from copy import deepcopy

import aiosqlite
import pytest

import test_investigation_fixed_part_answers_service as shared
from test_investigation_fixed_part_answers_v17_service import GroupRuntime
from potluck_local.services import investigations as service
from potluck_local.services import investigation_question_part_contracts as contracts


def test_distinct_otherwise_valid_plan_cannot_replace_existing_binding(tmp_path,monkeypatch):
    async def run():
        monkeypatch.setattr(shared,'MODE','fixed-part-selection-v17')
        monkeypatch.setattr(shared,'FixedRuntime',GroupRuntime)
        path,scope,_,_,_=await shared.setup(tmp_path,monkeypatch)
        runtime=GroupRuntime();monkeypatch.setattr(service,'get_runtime',lambda:runtime)
        original=await shared.create(scope)
        before=deepcopy(await service.get_investigation(scope,original['id']))
        other=shared.part_plans.prepare(shared.QUESTION,
            ['What exact expiry date is specified?','What approval or restriction governs access?'],
            origin='fixture_supplied')
        shared.part_plans.validate(other,question=shared.QUESTION,request=shared.QUESTION)
        assert other['plan_sha256']!=original['provenance']['question_part_plan']['plan_sha256']
        async with aiosqlite.connect(path) as db:
            await db.execute('BEGIN IMMEDIATE')
            cursor=await db.execute('SELECT * FROM investigations WHERE id=?',(original['id'],))
            snapshot=await cursor.fetchone()
            with pytest.raises(ValueError,match='binding failed'):
                await contracts.persist_in_transaction(db,scope,original['id'],other,
                                                       version='fixed-part-selection-v17')
            cursor=await db.execute('SELECT * FROM investigations WHERE id=?',(original['id'],))
            assert await cursor.fetchone()==snapshot
            await db.rollback()
        assert await service.get_investigation(scope,original['id'])==before
        assert len(runtime.calls)==2
    asyncio.run(run())
