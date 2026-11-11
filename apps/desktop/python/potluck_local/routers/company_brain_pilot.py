"""Explicit local-owner pilot for reviewed Company Brain evidence."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from potluck_local.services import evidence_lookup, investigation_request_parts, reviewed_knowledge, source_collections
from potluck_local.services import investigation_readable_presentation as readable
from potluck_local.services.investigations import _privacy, create_investigation
from potluck_local.services.memory_db import get_memory_db_path, init_memory_db
from potluck_local.routers.investigations import _error

router = APIRouter(prefix='/company-brain/pilot', tags=['company-brain-pilot'])

class Request(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workspace: str = Field(min_length=1, max_length=256)

class Scope(Request):
    collection_ids: list[str] = Field(min_length=1, max_length=100)
    def scope(self) -> evidence_lookup.EvidenceScope:
        return evidence_lookup.EvidenceScope(self.workspace, tuple(self.collection_ids))

class Register(Request):
    name: str = Field(min_length=1, max_length=256)
    folder: str = Field(min_length=1, max_length=4096)
    scan_confirmed: StrictBool

class Scan(Request):
    scan_confirmed: StrictBool

class Evidence(Scope):
    query: str = Field(min_length=1, max_length=1000)

class Claims(Scope):
    status: Literal['proposed', 'approved', 'superseded'] = 'proposed'

class Proposal(Scope):
    claim_key: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=16000)
    citation_ids: list[str] = Field(min_length=1, max_length=16)

class Review(Scope):
    reviewer: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=4000)

class Supersede(Review):
    replacement_id: str = Field(min_length=1, max_length=128)

class Ask(Scope):
    question: str = Field(min_length=1, max_length=500)
    model_slug: str = Field(min_length=1, max_length=128)
    pilot_confirmed: StrictBool
    focused_passages: StrictBool = False

class AskReviewed(Scope):
    question: str = Field(min_length=1, max_length=500)
    model_slug: str = Field(min_length=1, max_length=128)
    pilot_confirmed: StrictBool
    question_parts: list[StrictStr] = Field(min_length=1, max_length=6)
    question_parts_confirmed: StrictBool

def _pilot_error(exc: Exception) -> HTTPException:
    messages = {
        'Collection name already exists': 'A collection with this name already exists; scan it or choose another name.',
        'Choose a real absolute directory': 'Choose an existing absolute directory without symbolic links.',
        'Source citation unavailable': 'The source citation is unavailable or stale; scan and select evidence again.',
        'Use supersede to replace the current claim': 'An approved claim already uses this key; supersede it instead.',
        'Collection name required': 'Enter a collection name without leading or trailing whitespace.',
    }
    message = messages.get(str(exc)) if isinstance(exc, ValueError) else None
    return HTTPException(status_code=400, detail=message) if message else _error(exc)

async def _start() -> None:
    _privacy()
    await init_memory_db()

async def _ready(scope: evidence_lookup.EvidenceScope) -> None:
    async with aiosqlite.connect(get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        ready, stale = await evidence_lookup._ready(db, scope)
        if stale or len(ready) != len(scope.collection_ids):
            raise ValueError('All selected collections must be ready and recent')

async def _source(scope: evidence_lookup.EvidenceScope, citation_id: str) -> dict[str, Any]:
    source = await evidence_lookup.resolve_citation(scope, citation_id)
    if not source or source.get('kind') != 'source_chunk':
        raise ValueError('Source citation unavailable')
    return source

@router.post('/collections')
async def register(body: Register) -> dict[str, Any]:
    try:
        await _start()
        if not body.scan_confirmed or not body.workspace.strip() or body.workspace != body.workspace.strip():
            raise ValueError('Explicit confirmation required')
        if not body.name.strip() or body.name != body.name.strip():
            raise ValueError('Collection name required')
        root = Path(body.folder)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir() or root.resolve() != root:
            raise ValueError('Choose a real absolute directory')
        cid = str(uuid.uuid4())
        async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
            await db.execute('BEGIN IMMEDIATE')
            cursor = await db.execute('SELECT 1 FROM source_collections WHERE workspace=? AND name=?', (body.workspace, body.name))
            if await cursor.fetchone():
                raise ValueError('Collection name already exists')
            await db.execute('INSERT INTO source_collections (id, workspace, name, root_path, includes, excludes, created_at) VALUES (?,?,?,?,?,?,?)',
                (cid, body.workspace, body.name, str(root), json.dumps(('**/*.md',)), json.dumps(source_collections.DEFAULT_EXCLUDES), source_collections._now()))
            await db.commit()
        return {'collection_id': cid, 'status': 'pending'}
    except (ValueError, PermissionError, LookupError, OSError) as exc:
        raise _pilot_error(exc) from None

@router.post('/collections/{collection_id}/scan')
async def scan(collection_id: str, body: Scan) -> dict[str, Any]:
    try:
        await _start()
        evidence_lookup.EvidenceScope(body.workspace, (collection_id,))
        if not body.scan_confirmed:
            raise ValueError('Explicit confirmation required')
        return asdict(await source_collections.scan_collection(body.workspace, collection_id))
    except (ValueError, PermissionError, LookupError, OSError) as exc:
        raise _pilot_error(exc) from None

@router.post('/evidence')
async def evidence(body: Evidence) -> dict[str, Any]:
    try:
        await _start()
        scope = body.scope()
        await _ready(scope)
        result = await evidence_lookup.lookup_evidence(scope, body.query, max_results=20, retrieval_version='v2')
        if result['scope_status']['ready'] != len(scope.collection_ids):
            raise ValueError('All selected collections must be ready and recent')
        return result
    except (ValueError, PermissionError, LookupError) as exc:
        raise _pilot_error(exc) from None

@router.post('/claims/list')
async def claims(body: Claims) -> dict[str, Any]:
    try:
        await _start()
        scope = body.scope()
        await _ready(scope)
        selected = []
        async with aiosqlite.connect(get_memory_db_path()) as db:
            db.row_factory = aiosqlite.Row
            await db.execute('BEGIN')
            placeholders = ','.join('?' for _ in scope.collection_ids)
            cursor = await db.execute(
                'SELECT * FROM reviewed_claims c WHERE workspace=? AND status=? '
                'AND EXISTS (SELECT 1 FROM reviewed_claim_sources r WHERE r.claim_id=c.id) '
                f'AND NOT EXISTS (SELECT 1 FROM reviewed_claim_sources r WHERE r.claim_id=c.id AND r.collection_id NOT IN ({placeholders})) '
                'ORDER BY created_at, id LIMIT 101', (body.workspace, body.status, *scope.collection_ids))
            rows = await cursor.fetchall()
            for raw in rows[:100]:
                row = dict(raw)
                refs = await reviewed_knowledge._refs(db, row['id'])
                if await reviewed_knowledge._valid(db, body.workspace, set(scope.collection_ids), refs):
                    row['citations'] = refs
                    selected.append(row)
        for row in selected:
            row['sources'] = [await _source(scope, f"source:{r['chunk_id']}:{r['revision_id']}") for r in row['citations']]
        await _ready(scope)
        return {'claims': selected, 'truncated': len(rows) > 100}
    except (ValueError, PermissionError, LookupError) as exc:
        raise _pilot_error(exc) from None

@router.post('/claims')
async def propose(body: Proposal) -> dict[str, str]:
    try:
        await _start()
        scope = body.scope()
        await _ready(scope)
        if len(set(body.citation_ids)) != len(body.citation_ids):
            raise ValueError('Duplicate citations')
        refs = []
        for citation in body.citation_ids:
            source = await _source(scope, citation)
            refs.append({k: source[k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')})
        cid = await reviewed_knowledge.propose_claim(body.workspace, body.collection_ids, body.claim_key, body.content, refs)
        return {'claim_id': cid, 'status': 'proposed'}
    except (ValueError, PermissionError, LookupError) as exc:
        raise _pilot_error(exc) from None

@router.post('/claims/{claim_id}/approve')
async def approve(claim_id: str, body: Review) -> dict[str, bool]:
    try:
        await _start()
        await _ready(body.scope())
        await reviewed_knowledge.approve_claim(body.workspace, body.collection_ids, claim_id, body.reviewer, body.reason)
        return {'ok': True}
    except (ValueError, PermissionError, LookupError) as exc:
        raise _pilot_error(exc) from None

@router.post('/claims/{claim_id}/supersede')
async def supersede(claim_id: str, body: Supersede) -> dict[str, bool]:
    try:
        await _start()
        await _ready(body.scope())
        await reviewed_knowledge.supersede_claim(body.workspace, body.collection_ids, claim_id, body.replacement_id, body.reviewer, body.reason)
        return {'ok': True}
    except (ValueError, PermissionError, LookupError) as exc:
        raise _pilot_error(exc) from None

@router.post('/ask')
async def ask(body: Ask) -> dict[str, Any]:
    try:
        await _start()
        scope = body.scope()
        if not body.pilot_confirmed:
            raise ValueError('Explicit confirmation required')
        await _ready(scope)
        return await create_investigation(scope, body.question, body.question, body.model_slug,
            template_version='investigation-v21', generation_profile='qwen3.8-27b-native-non-thinking',
            seed=7, reviewed_operations=[{'request': body.question, 'operation': 'answer'}],
            prompt_encoding='compact-calculation-v1', reliability_mode='bounded-evidence-v1',
            presentation_mode='reviewed-claim-selection-v4' if body.focused_passages else 'reviewed-claim-selection-v2', evidence_selection='reviewed-collection-v5')
    except (ValueError, PermissionError, LookupError) as exc:
        raise _pilot_error(exc) from None


@router.post('/ask-reviewed')
async def ask_reviewed(body: AskReviewed) -> dict[str, Any]:
    """Keep the fixed-parts contract opt-in and separate from the older pilot."""
    try:
        await _start()
        scope = body.scope()
        if not body.pilot_confirmed or not body.question_parts_confirmed:
            raise ValueError('Confirm the pilot and question parts before asking')
        plan = investigation_request_parts.prepare(
            body.question, body.question_parts, origin='reviewer_supplied')
        await _ready(scope)
        return readable.decorate(await create_investigation(
            scope, body.question, body.question, body.model_slug,
            template_version='investigation-v21',
            generation_profile='qwen3.8-27b-native-non-thinking', seed=7,
            reviewed_operations=[{'request': body.question, 'operation': 'answer'}],
            prompt_encoding='compact-calculation-v1', reliability_mode='bounded-evidence-v1',
            presentation_mode='fixed-part-selection-v32',
            evidence_selection='reviewed-collection-v5', question_part_plan=plan))
    except (ValueError, PermissionError, LookupError) as exc:
        raise _pilot_error(exc) from None
