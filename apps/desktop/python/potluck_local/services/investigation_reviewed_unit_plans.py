"""Current DB authority for opt-in reviewed source-unit rendering.

The nullable receipt map freezes the absence of a plan too. It is separate from
the pure renderer: a self-consistent catalog is not proof of an approved plan.
"""
from __future__ import annotations

from typing import Any
import json

import aiosqlite

from potluck_local.services import reviewed_claim_unit_plans as plans
from potluck_local.services import reviewed_knowledge as knowledge
from potluck_local.services import reviewed_source_unit_plan as primitive
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.memory_db import get_memory_db_path

VERSION = 'reviewed-claim-selection-v15'
VERSIONS = (VERSION, 'reviewed-claim-selection-v16', 'fixed-part-selection-v1', 'fixed-part-selection-v2', 'fixed-part-selection-v3', 'fixed-part-selection-v4', 'fixed-part-selection-v5', 'fixed-part-selection-v6', 'fixed-part-selection-v7', 'fixed-part-selection-v8', 'fixed-part-selection-v9', 'fixed-part-selection-v10', 'fixed-part-selection-v11', 'fixed-part-selection-v12', 'fixed-part-selection-v13', 'fixed-part-selection-v14', 'fixed-part-selection-v15', 'fixed-part-selection-v16', 'fixed-part-selection-v17', 'fixed-part-selection-v18', 'fixed-part-selection-v19', 'fixed-part-selection-v20', 'fixed-part-selection-v21', 'fixed-part-selection-v22', 'fixed-part-selection-v23', 'fixed-part-selection-v24', 'fixed-part-selection-v25', 'fixed-part-selection-v26', 'fixed-part-selection-v27', 'fixed-part-selection-v28', 'fixed-part-selection-v29', 'fixed-part-selection-v30', 'fixed-part-selection-v31', 'fixed-part-selection-v32')
RECEIPTS_KEY = 'reviewed_unit_plan_receipts'


async def persist_contract_in_transaction(
    db: aiosqlite.Connection, scope: EvidenceScope, investigation_id: str,
    *, version: str = VERSION,
) -> None:
    """Bind the mode separately from editable provenance on opt-in saves only."""
    if not db.in_transaction:
        raise ValueError('Existing transaction required')
    if version not in VERSIONS:
        raise ValueError('Invalid reviewed unit-plan mode')
    cursor = await db.execute('PRAGMA table_info(investigations)')
    if 'unit_plan_contract' not in {row[1] for row in await cursor.fetchall()}:
        await db.execute('ALTER TABLE investigations ADD COLUMN unit_plan_contract TEXT')
    cursor = await db.execute(
        "UPDATE investigations SET unit_plan_contract=? WHERE id=? AND workspace=? AND scope=? AND status='draft'",
        (version, investigation_id, scope.workspace, json.dumps(sorted(scope.collection_ids))),
    )
    if cursor.rowcount != 1:
        raise ValueError('Investigation contract binding failed')


async def _validate_stored_contract(
    db: aiosqlite.Connection, scope: EvidenceScope, item: dict[str, Any],
) -> None:
    investigation_id = item.get('id')
    if investigation_id is None:  # Creation validates before INSERT, then binds it.
        return
    cursor = await db.execute('PRAGMA table_info(investigations)')
    stored = None
    if 'unit_plan_contract' in {row[1] for row in await cursor.fetchall()}:
        cursor = await db.execute(
            'SELECT unit_plan_contract FROM investigations WHERE id=? AND workspace=? AND scope=?',
            (investigation_id, scope.workspace, json.dumps(sorted(scope.collection_ids))),
        )
        row = await cursor.fetchone()
        if not row:
            raise LookupError('Investigation unavailable')
        stored = row[0]
    declared = (item.get('provenance') or {}).get('presentation_mode')
    if (stored not in (None, *VERSIONS)
            or (stored in VERSIONS or declared in VERSIONS) and stored != declared):
        raise ValueError('Invalid stored reviewed unit-plan contract')


async def snapshot_in_transaction(
    db: aiosqlite.Connection, scope: EvidenceScope, evidence: list[dict[str, Any]],
) -> dict[str, dict[str, Any] | None]:
    """Resolve every claim and exact plan without committing caller writes."""
    if not db.in_transaction:
        raise ValueError('Existing transaction required')
    if not isinstance(scope, EvidenceScope):
        raise ValueError('Explicit evidence scope required')
    allowed = knowledge._scope(scope.workspace, scope.collection_ids)
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= primitive.MAX_CLAIMS:
        raise ValueError('Invalid reviewed unit-plan evidence')
    result: dict[str, dict[str, Any] | None] = {}
    ref_fields = ('collection_id', 'document_id', 'revision_id', 'chunk_id')
    for claim in evidence:
        if (not isinstance(claim, dict) or claim.get('kind') != 'reviewed_claim'
                or not isinstance(claim.get('id'), str)
                or claim.get('citation_id') != 'claim:' + claim['id']
                or claim.get('status') != 'approved'
                or claim['citation_id'] in result):
            raise ValueError('Invalid reviewed unit-plan claim')
        binding, content = await plans._binding(db, scope.workspace, allowed, claim['id'])
        if (claim.get('content') != content
                or claim.get('reviewer') != binding['claim_reviewer']
                or claim.get('reviewed_at') != binding['claim_reviewed_at']):
            raise ValueError('Reviewed source claim changed')
        sources = claim.get('sources')
        if not isinstance(sources, list) or not sources:
            raise ValueError('Invalid reviewed source references')
        try:
            refs = [{key: source[key] for key in ref_fields} for source in sources]
            refs.sort(key=lambda ref: tuple(ref[key] for key in ref_fields))
        except (KeyError, TypeError) as exc:
            raise ValueError('Invalid reviewed source references') from exc
        if refs != binding['source_refs']:
            raise ValueError('Reviewed source references changed')
        for source in sources:
            cursor = await db.execute(
                'SELECT content FROM source_chunks WHERE id=? AND document_id=?',
                (source['chunk_id'], source['document_id']),
            )
            row = await cursor.fetchone()
            if (not row or source.get('content') != row[0]
                    or source.get('citation_id') != f"source:{source['chunk_id']}:{source['revision_id']}"):
                raise ValueError('Reviewed source content changed')
        result[claim['citation_id']] = await plans.get_approved_plan_in_transaction(
            db, scope.workspace, scope.collection_ids, claim['id'],
        )
    return result


async def snapshot(scope: EvidenceScope, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Finish this short transaction before performing any model generation."""
    async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        receipts = await snapshot_in_transaction(db, scope, evidence)
        await db.commit()
        return receipts


async def validate_in_transaction(
    db: aiosqlite.Connection, scope: EvidenceScope, item: dict[str, Any],
) -> None:
    """Recheck source/plan liveness in the save/read/review transaction."""
    if not db.in_transaction:
        raise ValueError('Existing transaction required')
    await _validate_stored_contract(db, scope, item)
    provenance = item.get('provenance') or {}
    trace = provenance.get('reviewed_claim_selection')
    if provenance.get('presentation_mode') not in VERSIONS:
        if RECEIPTS_KEY in provenance or (isinstance(trace, dict) and trace.get('version') in VERSIONS):
            raise ValueError('Invalid reviewed unit-plan mode')
        return
    receipts = provenance.get(RECEIPTS_KEY)
    if not isinstance(receipts, dict):
        raise ValueError('Missing reviewed unit-plan receipts')
    current = await snapshot_in_transaction(db, scope, item['evidence'])
    if receipts != current:
        raise LookupError('Investigation source plans are no longer available')
