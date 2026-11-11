"""Independent, transaction-bound fixed question plan for internal investigations."""
from __future__ import annotations

import json

from . import investigation_request_parts as parts

VERSION = 'fixed-part-selection-v1'
VERSIONS = (VERSION, 'fixed-part-selection-v2', 'fixed-part-selection-v3', 'fixed-part-selection-v4', 'fixed-part-selection-v5', 'fixed-part-selection-v6', 'fixed-part-selection-v7', 'fixed-part-selection-v8', 'fixed-part-selection-v9', 'fixed-part-selection-v10', 'fixed-part-selection-v11', 'fixed-part-selection-v12', 'fixed-part-selection-v13', 'fixed-part-selection-v14', 'fixed-part-selection-v15', 'fixed-part-selection-v16', 'fixed-part-selection-v17', 'fixed-part-selection-v18', 'fixed-part-selection-v19', 'fixed-part-selection-v20', 'fixed-part-selection-v21', 'fixed-part-selection-v22', 'fixed-part-selection-v23', 'fixed-part-selection-v24', 'fixed-part-selection-v25', 'fixed-part-selection-v26', 'fixed-part-selection-v27', 'fixed-part-selection-v28', 'fixed-part-selection-v29', 'fixed-part-selection-v30', 'fixed-part-selection-v31', 'fixed-part-selection-v32')


async def persist_in_transaction(db, scope, investigation_id, plan, *, version=VERSION):
    if not db.in_transaction:
        raise ValueError('Existing transaction required')
    if version not in VERSIONS:
        raise ValueError('Invalid question-part contract mode')
    validated = parts.validate(plan)
    cursor = await db.execute('PRAGMA table_info(investigations)')
    if 'question_part_contract' not in {row[1] for row in await cursor.fetchall()}:
        await db.execute('ALTER TABLE investigations ADD COLUMN question_part_contract TEXT')
    cursor = await db.execute(
        "UPDATE investigations SET question_part_contract=? WHERE id=? AND workspace=? AND scope=? "
        "AND status='draft' AND question_part_contract IS NULL",
        (json.dumps({'version': version, 'plan': validated}), investigation_id,
         scope.workspace, json.dumps(sorted(scope.collection_ids))),
    )
    if cursor.rowcount != 1:
        raise ValueError('Question-part contract binding failed')


async def validate_in_transaction(db, scope, item):
    if not db.in_transaction:
        raise ValueError('Existing transaction required')
    p = item.get('provenance') or {}
    mode = p.get('presentation_mode')
    declared = mode in VERSIONS
    answers = (item.get('brief') or {}).get('answers', [])
    marked = any(isinstance(a, dict) and isinstance(a.get('passage_presentation'), dict)
                 and a['passage_presentation'].get('version') in VERSIONS for a in answers)
    if not declared and ('question_part_plan' in p or marked):
        raise ValueError('Unexpected question-part plan')
    if item.get('id') is None:
        return  # Original is replayed before INSERT; binding is written atomically.
    stored = None
    cursor = await db.execute('PRAGMA table_info(investigations)')
    if 'question_part_contract' in {row[1] for row in await cursor.fetchall()}:
        cursor = await db.execute(
            'SELECT question_part_contract FROM investigations WHERE id=? AND workspace=? AND scope=?',
            (item['id'], scope.workspace, json.dumps(sorted(scope.collection_ids))),
        )
        row = await cursor.fetchone()
        if not row:
            raise LookupError('Investigation unavailable')
        stored = row[0]
    if not declared and stored is None:
        return
    if not declared or stored is None:
        raise ValueError('Invalid stored question-part contract')
    try:
        contract = json.loads(stored)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid stored question-part contract') from exc
    if contract != {'version': mode, 'plan': p.get('question_part_plan')}:
        raise ValueError('Question-part contract changed')
