"""Local-owner source registration and atomic Markdown ingestion.

Evidence stays outside personal memory search and peer routes. The separate
evidence service provides explicitly scoped retrieval. Workspace identifiers
organize evidence; they are not credentials. This module exposes no network endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass
from collections.abc import Iterator
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import aiosqlite

from potluck_local.services.memory_db import get_memory_db_path, init_memory_db
from potluck_local.services.memory_safety import redact_credentials
from potluck_local.services.source_schema import TOMBSTONE_SQL
from potluck_local.services.investigations import invalidate
from potluck_local.services.vault_ingestion import chunk_markdown

PARSER_VERSION = 'markdown-evidence-v1'
MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_EXCLUDES = ('.git/**', '.obsidian/**', 'archive/**', '**/archive/**')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _patterns(values: tuple[str, ...]) -> tuple[str, ...]:
    if any(not p or p.startswith('/') or '\\' in p or '..' in p.split('/') for p in values):
        raise ValueError('Patterns must be non-empty relative POSIX globs without parent traversal')
    return values


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    # A leading **/ also matches files directly beneath the selected root.
    return any(fnmatchcase(path, p) or (p.startswith('**/') and fnmatchcase(path, p[3:]))
               for p in patterns)


async def register_collection(
    workspace: str, name: str, folder: Path, *,
    includes: tuple[str, ...] = ('**/*.md',),
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
) -> str:
    """Register once; explicit repeat registration may update selection rules."""
    if not workspace.strip() or not name.strip() or not includes:
        raise ValueError('Workspace, collection name and include patterns are required')
    root = folder.expanduser().absolute()
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ValueError('Choose an existing directory through its real, non-symlink path')
    include_json = json.dumps(_patterns(includes))
    exclude_json = json.dumps(_patterns(excludes))
    async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
        await db.execute('PRAGMA foreign_keys=ON')
        await db.execute('BEGIN IMMEDIATE')
        cursor = await db.execute(
            'SELECT id, root_path, includes, excludes FROM source_collections '
            'WHERE workspace = ? AND name = ?', (workspace, name),
        )
        row = await cursor.fetchone()
        if row:
            if row[1] != str(root):
                raise ValueError('Collection root cannot change; register a new collection')
            if (row[2], row[3]) != (include_json, exclude_json):
                await invalidate(db, 'collection_id=?', (row[0],))
                # Revoke all payloads derived from this collection, including history.
                await db.execute(TOMBSTONE_SQL +
                                 'id IN (SELECT claim_id FROM reviewed_claim_sources '
                                 'WHERE collection_id=?)',
                                 (_now(), 'collection_reconfigured', row[0]))
                # Changed rules immediately revoke derived content; the next scan rebuilds it.
                await db.execute(
                    'DELETE FROM source_chunks WHERE document_id IN '
                    '(SELECT id FROM source_documents WHERE collection_id = ?)', (row[0],),
                )
                await db.execute('DELETE FROM source_documents WHERE collection_id = ?', (row[0],))
                await db.execute(
                    "UPDATE source_collections SET includes=?, excludes=?, scan_status='pending', "
                    'last_error=NULL WHERE id=?', (include_json, exclude_json, row[0]),
                )
            await db.commit()
            return str(row[0])
        collection_id = str(uuid.uuid4())
        await db.execute(
            'INSERT INTO source_collections '
            '(id, workspace, name, root_path, includes, excludes, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (collection_id, workspace, name, str(root), include_json, exclude_json, _now()),
        )
        await db.commit()
        return collection_id


def _read_files(
    root: Path, includes: tuple[str, ...], excludes: tuple[str, ...],
) -> Iterator[tuple[str, str]]:
    if not root.is_dir() or root.is_symlink() or root.resolve() != root:
        raise ValueError('Source root is missing or no longer the registered real directory')

    def fail(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        parent = Path(directory)
        dirs[:] = sorted(
            d for d in dirs if not (parent / d).is_symlink()
            and not _matches((parent / d).relative_to(root).as_posix() + '/', excludes)
        )
        for name in sorted(files):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if (path.suffix != '.md' or not _matches(relative, includes)
                    or _matches(relative, excludes)):
                continue
            if path.is_symlink() or path.resolve() != path or not path.is_file():
                continue
            # O_NOFOLLOW closes the final-component symlink race on supporting hosts.
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError('Source is no longer a regular file')
                raw = source.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError('Source exceeds the 2 MiB file limit')
            if path.resolve() != path:
                raise ValueError('Source path changed during scan')
            yield relative, raw.decode('utf-8')


@dataclass
class CollectionScan:
    collection_id: str
    files_scanned: int = 0
    files_added: int = 0
    files_updated: int = 0
    files_removed: int = 0
    chunks_inserted: int = 0


async def scan_collection(workspace: str, collection_id: str) -> CollectionScan:
    """Replace one collection atomically; failed scans keep the last complete snapshot."""
    result = CollectionScan(collection_id)
    try:
        async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
            await db.execute('PRAGMA foreign_keys=ON')
            await db.execute('BEGIN IMMEDIATE')
            cursor = await db.execute(
                'SELECT root_path, includes, excludes FROM source_collections '
                'WHERE id=? AND workspace=?', (collection_id, workspace),
            )
            collection = await cursor.fetchone()
            if not collection:
                raise LookupError('Collection not found in this workspace')
            cursor = await db.execute(
                'SELECT relative_path, content_sha, parser_version FROM source_documents '
                'WHERE collection_id=?', (collection_id,),
            )
            known = {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}
            seen: set[str] = set()
            for relative, text in _read_files(
                Path(collection[0]), tuple(json.loads(collection[1])),
                tuple(json.loads(collection[2])),
            ):
                seen.add(relative)
                result.files_scanned += 1
                sha = hashlib.sha256(text.encode()).hexdigest()
                if known.get(relative) == (sha, PARSER_VERSION):
                    continue
                document_id = str(uuid.uuid5(uuid.UUID(collection_id), relative))
                safe_text = redact_credentials(text)
                revision_id = str(uuid.uuid5(
                    uuid.UUID(document_id), PARSER_VERSION + ':'
                    + hashlib.sha256(safe_text.encode()).hexdigest(),
                ))
                await invalidate(db, 'collection_id=?', (collection_id,))
                await db.execute(TOMBSTONE_SQL +
                                 'id IN (SELECT claim_id FROM reviewed_claim_sources '
                                 'WHERE document_id=?)',
                                 (_now(), 'source_changed', document_id))
                await db.execute('DELETE FROM source_chunks WHERE document_id=?', (document_id,))
                await db.execute(
                    'INSERT INTO source_documents '
                    '(id, collection_id, relative_path, content_sha, revision_id, parser_version, '
                    'updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET '
                    'content_sha=excluded.content_sha, revision_id=excluded.revision_id, '
                    'parser_version=excluded.parser_version, updated_at=excluded.updated_at',
                    (document_id, collection_id, relative, sha, revision_id,
                     PARSER_VERSION, _now()),
                )
                chunks = chunk_markdown(safe_text)
                for ordinal, chunk in enumerate(chunks):
                    chunk_id = str(uuid.uuid5(uuid.UUID(revision_id), str(ordinal)))
                    await db.execute(
                        'INSERT INTO source_chunks '
                        '(id, document_id, ordinal, anchor, heading, content) VALUES (?,?,?,?,?,?)',
                        (chunk_id, document_id, ordinal, chunk.anchor, chunk.heading, chunk.body),
                    )
                result.chunks_inserted += len(chunks)
                if relative in known:
                    result.files_updated += 1
                else:
                    result.files_added += 1
            for relative in known.keys() - seen:
                cur = await db.execute('SELECT id FROM source_documents WHERE collection_id=? AND relative_path=?', (collection_id, relative))
                removed = await cur.fetchone()
                if removed:
                    await invalidate(db, 'collection_id=?', (collection_id,))
                await db.execute(TOMBSTONE_SQL +
                                 'id IN (SELECT r.claim_id FROM reviewed_claim_sources r '
                                 'JOIN source_documents d ON d.id=r.document_id '
                                 'WHERE d.collection_id=? AND d.relative_path=?)',
                                 (_now(), 'source_removed', collection_id, relative))
                await db.execute(
                    'DELETE FROM source_documents WHERE collection_id=? AND relative_path=?',
                    (collection_id, relative),
                )
                result.files_removed += 1
            await db.execute(
                "UPDATE source_collections SET last_scan_at=?, "
                "scan_status='ready', last_error=NULL "
                'WHERE id=?', (_now(), collection_id),
            )
            await db.commit()
    except (OSError, ValueError):
        async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
            await db.execute(
                "UPDATE source_collections SET scan_status='failed', "
                "last_error='Scan failed; previous snapshot retained. "
                "Check source access and UTF-8 files.' "
                'WHERE id=? AND workspace=?', (collection_id, workspace),
            )
            await db.commit()
        raise
    return result


async def collection_status(workspace: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            'SELECT c.*, (SELECT COUNT(*) FROM source_documents d WHERE d.collection_id=c.id) '
            'AS file_count, (SELECT COUNT(*) FROM source_chunks k JOIN source_documents d '
            'ON k.document_id=d.id WHERE d.collection_id=c.id) AS chunk_count '
            'FROM source_collections c WHERE workspace=? ORDER BY c.name', (workspace,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def remove_collection(workspace: str, collection_id: str) -> None:
    """Remove registered evidence only; never delete source files."""
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await db.execute('PRAGMA foreign_keys=ON')
        await db.execute('BEGIN IMMEDIATE')
        exists = await db.execute('SELECT 1 FROM source_collections WHERE id=? AND workspace=?',
                                  (collection_id, workspace))
        if not await exists.fetchone():
            raise LookupError('Collection not found in this workspace')
        await invalidate(db, 'collection_id=?', (collection_id,))
        await db.execute(TOMBSTONE_SQL +
                         'workspace=? AND id IN '
                         '(SELECT claim_id FROM reviewed_claim_sources '
                         'WHERE collection_id=?)',
                         (_now(), 'collection_removed', workspace, collection_id))
        cursor = await db.execute('DELETE FROM source_collections WHERE id=? AND workspace=?',
                                  (collection_id, workspace))
        if cursor.rowcount != 1:
            raise LookupError('Collection not found in this workspace')
        await db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    register = commands.add_parser('register')
    register.add_argument('name')
    register.add_argument('folder', type=Path)
    register.add_argument('--include', action='append')
    register.add_argument('--exclude', action='append', default=[])
    commands.add_parser('status')
    for command in ('scan', 'remove'):
        commands.add_parser(command).add_argument('collection_id')
    args = parser.parse_args()

    async def run() -> Any:
        await init_memory_db()
        if args.command == 'register':
            return {'collection_id': await register_collection(
                args.workspace, args.name, args.folder,
                includes=tuple(args.include or ['**/*.md']),
                excludes=DEFAULT_EXCLUDES + tuple(args.exclude),
            )}
        if args.command == 'scan':
            return asdict(await scan_collection(args.workspace, args.collection_id))
        if args.command == 'remove':
            await remove_collection(args.workspace, args.collection_id)
            return {'removed': args.collection_id}
        return await collection_status(args.workspace)

    try:
        print(json.dumps(asyncio.run(run()), indent=2))
    except (OSError, ValueError, LookupError) as exc:
        parser.exit(1, f'{type(exc).__name__}: source operation failed; '
                    'check registration and files.\n')


if __name__ == '__main__':
    main()
