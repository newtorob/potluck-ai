"""Manual review of source-backed claims; caller supplies its collection scope.

This local service does not authenticate callers or inspect the filesystem. Source
freshness reflects the last scan, and a failed scan makes its claims unavailable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

import aiosqlite

from potluck_local.services.memory_db import get_memory_db_path, init_memory_db
from potluck_local.services.memory_safety import redact_credentials
from potluck_local.services.source_collections import _now
from potluck_local.services.source_schema import TOMBSTONE_SQL


def _scope(workspace: str, allowed: list[str] | tuple[str, ...]) -> set[str]:
    if (not isinstance(workspace, str) or not workspace.strip() or workspace != workspace.strip()
            or not isinstance(allowed, (list, tuple)) or not allowed
            or any(not isinstance(x, str) or not x or x != x.strip() or x == '*'
                   for x in allowed) or len(set(allowed)) != len(allowed)):
        raise ValueError('Workspace and explicit collection scope are required')
    return set(allowed)


async def _valid(db: aiosqlite.Connection, workspace: str, allowed: set[str],
                 citations: list[dict[str, str]]) -> bool:
    if not isinstance(citations, list) or not citations or len(citations) > 16:
        return False
    seen: set[tuple[str, ...]] = set()
    for ref in citations:
        if (not isinstance(ref, dict)
                or set(ref) != {'collection_id', 'document_id', 'revision_id', 'chunk_id'}):
            return False
        if any(not isinstance(value, str) or not value or value != value.strip()
               for value in ref.values()):
            return False
        identity = tuple(ref[key] for key in
                         ('collection_id', 'document_id', 'revision_id', 'chunk_id'))
        if identity in seen:
            return False
        seen.add(identity)
        if ref['collection_id'] not in allowed:
            return False
        cursor = await db.execute(
            'SELECT 1 FROM source_collections c JOIN source_documents d ON d.collection_id=c.id '
            'JOIN source_chunks k ON k.document_id=d.id WHERE c.workspace=? AND c.id=? '
            "AND c.scan_status='ready' AND d.id=? AND d.revision_id=? AND k.id=?",
            (workspace, ref['collection_id'], ref['document_id'],
             ref['revision_id'], ref['chunk_id']))
        if not await cursor.fetchone():
            return False
    return True


async def _refs(db: aiosqlite.Connection, claim_id: str) -> list[dict[str, str]]:
    cursor = await db.execute('SELECT collection_id, document_id, revision_id, chunk_id '
                              'FROM reviewed_claim_sources WHERE claim_id=? ORDER BY chunk_id',
                              (claim_id,))
    return [dict(zip(('collection_id', 'document_id', 'revision_id', 'chunk_id'), row))
            for row in await cursor.fetchall()]


async def _authorized(db: aiosqlite.Connection, workspace: str, allowed: set[str],
                      refs: list[dict[str, str]]) -> bool:
    if not refs:
        return False
    for ref in refs:
        collection = ref['collection_id']
        if collection not in allowed:
            return False
        cursor = await db.execute(
            'SELECT 1 FROM source_collections WHERE id=? AND workspace=?',
            (collection, workspace))
        if not await cursor.fetchone():
            return False
    return True


async def propose_claim(workspace: str, allowed_collection_ids: list[str] | tuple[str, ...],
                        claim_key: str, content: str, citations: list[dict[str, str]]) -> str:
    allowed = _scope(workspace, allowed_collection_ids)
    if (not isinstance(claim_key, str) or not claim_key.strip()
            or len(claim_key) > 256 or not isinstance(content, str)
            or not content.strip() or len(content) > 16000):
        raise ValueError('Claim key and content are required')
    claim_key = redact_credentials(claim_key)
    content = redact_credentials(content)
    async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
        await db.execute('PRAGMA foreign_keys=ON')
        await db.execute('BEGIN IMMEDIATE')
        if not await _valid(db, workspace, allowed, citations):
            raise ValueError('Citations are unavailable or outside scope')
        claim_id = str(uuid.uuid4())
        await db.execute('INSERT INTO reviewed_claims '
                         '(id, workspace, claim_key, content, status, created_at) '
                         "VALUES (?,?,?,?, 'proposed', ?)",
                         (claim_id, workspace, claim_key, content, _now()))
        for ref in citations:
            await db.execute('INSERT INTO reviewed_claim_sources VALUES (?,?,?,?,?)',
                             (claim_id, ref['collection_id'], ref['document_id'],
                              ref['revision_id'], ref['chunk_id']))
        await db.commit()
        return claim_id


async def _claim(db: aiosqlite.Connection, workspace: str, allowed: set[str],
                 claim_id: str) -> tuple[Any, ...]:
    cursor = await db.execute('SELECT id, claim_key, status FROM reviewed_claims '
                              'WHERE workspace=? AND id=?', (workspace, claim_id))
    row = await cursor.fetchone()
    if not row or not await _valid(db, workspace, allowed, await _refs(db, claim_id)):
        raise LookupError('Claim unavailable in this scope')
    return row


def _review(reviewer: str, reason: str) -> None:
    if (not isinstance(reviewer, str) or not reviewer.strip()
            or len(reviewer) > 256 or not isinstance(reason, str)
            or not reason.strip() or len(reason) > 4000):
        raise ValueError('Reviewer and reason are required')


async def approve_claim(workspace: str, allowed_collection_ids: list[str] | tuple[str, ...],
                        claim_id: str, reviewer: str, reason: str) -> None:
    allowed = _scope(workspace, allowed_collection_ids)
    _review(reviewer, reason)
    reviewer, reason = redact_credentials(reviewer), redact_credentials(reason)
    async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        row = await _claim(db, workspace, allowed, claim_id)
        if row[2] != 'proposed':
            raise ValueError('Only proposed claims can be approved')
        cursor = await db.execute(
            "SELECT 1 FROM reviewed_claims WHERE workspace=? AND claim_key=? AND status='approved'",
            (workspace, row[1]))
        if await cursor.fetchone():
            raise ValueError('Use supersede to replace the current claim')
        await db.execute(
            "UPDATE reviewed_claims SET status='approved', reviewer=?, reason=?, "
            'reviewed_at=? WHERE id=?', (reviewer, reason, _now(), claim_id))
        await db.commit()


async def supersede_claim(workspace: str, allowed_collection_ids: list[str] | tuple[str, ...],
                          current_id: str, replacement_id: str, reviewer: str, reason: str) -> None:
    allowed = _scope(workspace, allowed_collection_ids)
    _review(reviewer, reason)
    reviewer, reason = redact_credentials(reviewer), redact_credentials(reason)
    async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        old = await _claim(db, workspace, allowed, current_id)
        new = await _claim(db, workspace, allowed, replacement_id)
        if old[2] != 'approved' or new[2] != 'proposed' or old[1] != new[1]:
            raise ValueError('Supersession requires an approved claim and matching proposal')
        await db.execute(
            "UPDATE reviewed_claims SET status='superseded', superseded_by=?, "
            'superseded_at=?, superseded_reviewer=?, superseded_reason=? WHERE id=?',
            (replacement_id, _now(), reviewer, reason, current_id))
        await db.execute(
            "UPDATE reviewed_claims SET status='approved', reviewer=?, reason=?, "
            'reviewed_at=? WHERE id=?', (reviewer, reason, _now(), replacement_id))
        await db.commit()


async def list_claims(workspace: str, allowed_collection_ids: list[str] | tuple[str, ...],
                      status: str = 'approved') -> list[dict[str, Any]]:
    allowed = _scope(workspace, allowed_collection_ids)
    if status not in ('approved', 'proposed', 'superseded', 'deleted'):
        raise ValueError('Invalid status')
    async with aiosqlite.connect(get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN')
        cursor = await db.execute(
            'SELECT * FROM reviewed_claims WHERE workspace=? AND status=? ORDER BY created_at, id',
            (workspace, status))
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            refs = await _refs(db, row['id'])
            valid = (await _authorized(db, workspace, allowed, refs) if status == 'deleted'
                     else await _valid(db, workspace, allowed, refs))
            if valid:
                result.append({**dict(row), 'citations': refs})
        return result


async def delete_claim(workspace: str, allowed_collection_ids: list[str] | tuple[str, ...],
                       claim_id: str) -> None:
    allowed = _scope(workspace, allowed_collection_ids)
    async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
        await db.execute('PRAGMA foreign_keys=ON')
        await db.execute('BEGIN IMMEDIATE')
        cursor = await db.execute('SELECT 1 FROM reviewed_claims WHERE id=? AND workspace=?',
                                  (claim_id, workspace))
        if not await cursor.fetchone() or not await _authorized(
                db, workspace, allowed, await _refs(db, claim_id)):
            raise LookupError('Claim unavailable in this scope')
        await db.execute(TOMBSTONE_SQL + 'id=? AND workspace=?',
                         (_now(), 'manual', claim_id, workspace))
        await db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--collection', action='append', required=True,
                        help='Allowed collection ID; repeat for each authorized collection')
    commands = parser.add_subparsers(dest='command', required=True)
    propose = commands.add_parser('propose')
    propose.add_argument('claim_key')
    propose.add_argument('content')
    propose.add_argument('--citation', action='append', required=True,
                         help='JSON object with collection_id, document_id, revision_id, chunk_id')
    for name in ('approve', 'supersede'):
        command = commands.add_parser(name)
        command.add_argument('claim_id')
        if name == 'supersede':
            command.add_argument('replacement_id')
        command.add_argument('--reviewer', required=True)
        command.add_argument('--reason', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--status', default='approved',
                         choices=('approved', 'proposed', 'superseded', 'deleted'))
    commands.add_parser('delete').add_argument('claim_id')
    args = parser.parse_args()

    async def run() -> Any:
        await init_memory_db()
        scope = args.collection
        if args.command == 'propose':
            refs = [json.loads(value) for value in args.citation]
            return {'id': await propose_claim(args.workspace, scope, args.claim_key,
                                              args.content, refs)}
        if args.command == 'approve':
            await approve_claim(args.workspace, scope, args.claim_id, args.reviewer, args.reason)
        elif args.command == 'supersede':
            await supersede_claim(args.workspace, scope, args.claim_id, args.replacement_id,
                                  args.reviewer, args.reason)
        elif args.command == 'delete':
            await delete_claim(args.workspace, scope, args.claim_id)
        else:
            return await list_claims(args.workspace, scope, args.status)
        return {'ok': True}

    try:
        print(json.dumps(asyncio.run(run()), indent=2))
    except (ValueError, LookupError) as exc:
        parser.exit(1, f'{type(exc).__name__}: operation failed; check scope and inputs.\n')


if __name__ == '__main__':
    main()
