"""Explicitly reviewed plans bound to current approved source claims.

Source freshness reflects the last collection scan. This service does not itself
provide caller authentication or assert semantic completeness of declared edges.
"""
from __future__ import annotations
import json
import uuid
from typing import Any


import aiosqlite

from potluck_local.services import reviewed_knowledge as knowledge
from potluck_local.services import reviewed_source_unit_plan as primitive
from potluck_local.services.memory_db import get_memory_db_path
from potluck_local.services.memory_safety import redact_credentials
from potluck_local.services.source_collections import _now

async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        'CREATE TABLE IF NOT EXISTS reviewed_claim_unit_plans ('
        'id TEXT PRIMARY KEY, workspace TEXT NOT NULL, claim_id TEXT NOT NULL, '
        'revision TEXT NOT NULL, status TEXT NOT NULL, plan_json TEXT NOT NULL, '
        'plan_sha256 TEXT NOT NULL, binding_json TEXT NOT NULL, reviewer TEXT, '
        'reason TEXT, reviewed_at TEXT, created_at TEXT NOT NULL, superseded_by TEXT, '
        'superseded_at TEXT, superseded_reviewer TEXT, superseded_reason TEXT)'
    )
    await db.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS reviewed_claim_unit_plans_one_approved '
        "ON reviewed_claim_unit_plans(workspace,claim_id) WHERE status='approved'"
    )
    # Source tombstones must erase plan labels and review notes as well. These
    # persistent triggers cover existing source/claim deletion paths without
    # requiring those services to load this optional feature.
    await db.execute('''CREATE TRIGGER IF NOT EXISTS reviewed_unit_plan_tombstone
        AFTER UPDATE OF status ON reviewed_claims WHEN NEW.status='deleted'
        BEGIN
            DELETE FROM reviewed_claim_unit_plans
            WHERE claim_id=NEW.id AND workspace=NEW.workspace;
        END''')
    await db.execute('''CREATE TRIGGER IF NOT EXISTS reviewed_unit_plan_delete
        AFTER DELETE ON reviewed_claims
        BEGIN
            DELETE FROM reviewed_claim_unit_plans
            WHERE claim_id=OLD.id AND workspace=OLD.workspace;
        END''')

def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

async def _binding(
    db: aiosqlite.Connection, workspace: str, scope: set[str], claim_id: str,
) -> tuple[dict[str, Any], str]:
    row = await knowledge._claim(db, workspace, scope, claim_id)
    if row[2] != 'approved':
        raise LookupError('Approved claim unavailable in this scope')
    cur = await db.execute(
        'SELECT content,reviewer,reviewed_at FROM reviewed_claims WHERE workspace=? AND id=?',
        (workspace, claim_id),
    )
    found = await cur.fetchone()
    if not found or not found[1] or (not found[2]):
        raise LookupError('Approved claim unavailable in this scope')
    refs = await knowledge._refs(db, claim_id)
    refs.sort(key=lambda r: (r['collection_id'], r['document_id'], r['revision_id'], r['chunk_id']))
    binding = {
        'claim_id': claim_id, 'content_sha256': primitive.text_hash(found[0]),
        'claim_reviewer': found[1], 'claim_reviewed_at': found[2], 'source_refs': refs,
    }
    return binding, found[0]

def _validate(plan: Any, content: str) -> tuple[str, str]:
    if redact_credentials(_json(plan)) != _json(plan):
        raise ValueError('Plan contains sensitive material')
    primitive.prepare([{'id': 'C', 'text': content, 'citations': ['source']}], {'C': plan})
    return (_json(plan), primitive.digest(plan))

def _usable(plan: Any, content: str) -> None:
    catalog = primitive.prepare([{'id': 'C', 'text': content, 'citations': ['source']}], {'C': plan})
    for unit in catalog['units']:
        primitive.render(catalog, [unit['id']])

async def _record(
    db: aiosqlite.Connection, workspace: str, scope: set[str],
    plan_id: str, expected_status: str | None = None,
) -> dict[str, Any]:
    cur = await db.execute(
        'SELECT id,claim_id,revision,status,plan_json,plan_sha256,binding_json,'
        'reviewer,reason,reviewed_at,created_at,superseded_by,superseded_at,'
        'superseded_reviewer,superseded_reason FROM reviewed_claim_unit_plans '
        'WHERE workspace=? AND id=?', (workspace, plan_id),
    )
    row = await cur.fetchone()
    if not row:
        raise LookupError('Plan unavailable in this scope')
    keys = (
        'id', 'claim_id', 'revision', 'status', 'plan_json', 'plan_sha256',
        'binding_json', 'reviewer', 'reason', 'reviewed_at', 'created_at',
        'superseded_by', 'superseded_at', 'superseded_reviewer', 'superseded_reason',
    )
    data = dict(zip(keys, row))
    binding, content = await _binding(db, workspace, scope, data['claim_id'])
    try:
        plan = json.loads(data['plan_json'])
        stored = json.loads(data['binding_json'])
        canonical, sha = _validate(plan, content)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Plan integrity check failed') from exc
    if (
        stored != binding or data['binding_json'] != _json(stored)
        or data['plan_json'] != canonical or sha != data['plan_sha256']
    ):
        raise ValueError('Plan binding or integrity changed')
    if expected_status and data['status'] != expected_status:
        raise ValueError('Plan status changed')
    try:
        for field in ('id', 'revision'):
            if str(uuid.UUID(data[field])) != data[field]:
                raise ValueError('Invalid plan identifier')
        if not data['created_at']:
            raise ValueError('Missing creation time')
        approval = (data['reviewer'], data['reason'], data['reviewed_at'])
        transition = (data['superseded_by'], data['superseded_at'],
                      data['superseded_reviewer'], data['superseded_reason'])
        if data['status'] == 'proposed':
            if any(x is not None for x in approval + transition):
                raise ValueError('Invalid proposed metadata')
        elif data['status'] in ('approved', 'superseded'):
            knowledge._review(data['reviewer'], data['reason'])
            if not data['reviewed_at']:
                raise ValueError('Missing review time')
            if data['status'] == 'approved' and any(x is not None for x in transition):
                raise ValueError('Invalid approved metadata')
            if data['status'] == 'superseded':
                knowledge._review(data['superseded_reviewer'], data['superseded_reason'])
                if (not data['superseded_at'] or not data['superseded_by']
                        or data['superseded_by'] == data['id']):
                    raise ValueError('Missing transition metadata')
                cur = await db.execute(
                    'SELECT claim_id,binding_json FROM reviewed_claim_unit_plans WHERE workspace=? AND id=?',
                    (workspace, data['superseded_by']),
                )
                successor = await cur.fetchone()
                if not successor or tuple(successor) != (data['claim_id'], data['binding_json']):
                    raise ValueError('Invalid plan successor')
        else:
            raise ValueError('Invalid plan status')
    except (TypeError, AttributeError) as exc:
        raise ValueError('Invalid plan metadata') from exc
    return {**data, 'plan': plan, 'binding': binding}

async def propose_plan(
    workspace: str, allowed_collection_ids: list[str] | tuple[str, ...],
    claim_id: str, plan: Any,
) -> str:
    scope = knowledge._scope(workspace, allowed_collection_ids)
    async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        await _ensure_schema(db)
        binding, content = await _binding(db, workspace, scope, claim_id)
        canonical, sha = _validate(plan, content)
        pid = str(uuid.uuid4())
        revision = str(uuid.uuid4())
        await db.execute(
            'INSERT INTO reviewed_claim_unit_plans '
            '(id,workspace,claim_id,revision,status,plan_json,plan_sha256,binding_json,created_at) '
            "VALUES(?,?,?,?,'proposed',?,?,?,?)",
            (pid, workspace, claim_id, revision, canonical, sha, _json(binding), _now()),
        )
        await db.commit()
        return pid

async def approve_plan(
    workspace: str, allowed_collection_ids: list[str] | tuple[str, ...],
    plan_id: str, reviewer: str, reason: str,
) -> None:
    scope = knowledge._scope(workspace, allowed_collection_ids)
    knowledge._review(reviewer, reason)
    reviewer, reason = (redact_credentials(reviewer), redact_credentials(reason))
    async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        await _ensure_schema(db)
        data = await _record(db, workspace, scope, plan_id, 'proposed')
        _, content = await _binding(db, workspace, scope, data['claim_id'])
        _usable(data['plan'], content)
        cur = await db.execute(
            'SELECT 1 FROM reviewed_claim_unit_plans WHERE workspace=? AND claim_id=? '
            "AND status='approved'", (workspace, data['claim_id']),
        )
        if await cur.fetchone():
            raise ValueError('Use supersede to replace the current plan')
        await db.execute(
            "UPDATE reviewed_claim_unit_plans SET status='approved',reviewer=?,reason=?,"
            'reviewed_at=? WHERE id=?', (reviewer, reason, _now(), plan_id),
        )
        await db.commit()

async def supersede_plan(
    workspace: str, allowed_collection_ids: list[str] | tuple[str, ...],
    current_id: str, replacement_id: str, reviewer: str, reason: str,
) -> None:
    scope = knowledge._scope(workspace, allowed_collection_ids)
    knowledge._review(reviewer, reason)
    reviewer, reason = (redact_credentials(reviewer), redact_credentials(reason))
    async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        await _ensure_schema(db)
        old = await _record(db, workspace, scope, current_id, 'approved')
        new = await _record(db, workspace, scope, replacement_id, 'proposed')
        _, content = await _binding(db, workspace, scope, new['claim_id'])
        _usable(new['plan'], content)
        if old['claim_id'] != new['claim_id'] or old['binding'] != new['binding']:
            raise ValueError('Replacement must have the same source binding')
        await db.execute(
            "UPDATE reviewed_claim_unit_plans SET status='superseded',superseded_by=?,"
            'superseded_at=?,superseded_reviewer=?,superseded_reason=? WHERE id=?',
            (replacement_id, _now(), reviewer, reason, current_id),
        )
        await db.execute(
            "UPDATE reviewed_claim_unit_plans SET status='approved',reviewer=?,reason=?,"
            'reviewed_at=? WHERE id=?', (reviewer, reason, _now(), replacement_id),
        )
        await db.commit()

async def get_approved_plan(
    workspace: str, allowed_collection_ids: list[str] | tuple[str, ...], claim_id: str,
) -> dict[str, Any] | None:
    async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        result = await get_approved_plan_in_transaction(db, workspace, allowed_collection_ids, claim_id)
        await db.commit()
        return result

async def get_approved_plan_in_transaction(
    db: aiosqlite.Connection, workspace: str,
    allowed_collection_ids: list[str] | tuple[str, ...], claim_id: str,
) -> dict[str, Any] | None:
    """Read and validate a current receipt on the caller's existing transaction."""
    if not db.in_transaction:
        raise ValueError('Existing transaction required')
    scope = knowledge._scope(workspace, allowed_collection_ids)
    await _ensure_schema(db)
    await _binding(db, workspace, scope, claim_id)
    cursor = await db.execute(
        'SELECT id FROM reviewed_claim_unit_plans WHERE workspace=? AND claim_id=?',
        (workspace, claim_id),
    )
    for (plan_id,) in await cursor.fetchall():
        await _record(db, workspace, scope, plan_id)
    cursor = await db.execute(
        'SELECT id FROM reviewed_claim_unit_plans WHERE workspace=? AND claim_id=? '
        "AND status='approved'", (workspace, claim_id),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    data = await _record(db, workspace, scope, row[0], 'approved')
    receipt_fields = (
        'id', 'revision', 'plan_sha256', 'plan', 'binding', 'reviewer', 'reason', 'reviewed_at',
    )
    return {key: data[key] for key in receipt_fields}

async def validate_receipt_in_transaction(
    db: aiosqlite.Connection, workspace: str,
    allowed_collection_ids: list[str] | tuple[str, ...], claim_id: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed if a previously saved receipt is no longer exactly current."""
    current = await get_approved_plan_in_transaction(db, workspace, allowed_collection_ids, claim_id)
    if current is None or current != receipt:
        raise ValueError('Plan receipt is no longer current')
    return current
