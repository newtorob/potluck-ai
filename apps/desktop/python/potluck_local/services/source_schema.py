"""Scoped source evidence and reviewed claims, separate from personal memories."""

SOURCE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS source_collections (
        id TEXT PRIMARY KEY,
        workspace TEXT NOT NULL,
        name TEXT NOT NULL,
        root_path TEXT NOT NULL,
        includes TEXT NOT NULL,
        excludes TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_scan_at TEXT,
        scan_status TEXT NOT NULL DEFAULT 'never',
        last_error TEXT,
        UNIQUE(workspace, name)
    )""",
    """CREATE TABLE IF NOT EXISTS source_documents (
        id TEXT PRIMARY KEY,
        collection_id TEXT NOT NULL REFERENCES source_collections(id) ON DELETE CASCADE,
        relative_path TEXT NOT NULL,
        content_sha TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        parser_version TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(collection_id, relative_path)
    )""",
    """CREATE TABLE IF NOT EXISTS source_chunks (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        anchor TEXT NOT NULL,
        heading TEXT NOT NULL,
        content TEXT NOT NULL,
        UNIQUE(document_id, ordinal)
    )""",
    """CREATE TABLE IF NOT EXISTS reviewed_claims (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, claim_key TEXT NOT NULL,
        content TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN
            ('proposed', 'approved', 'superseded', 'deleted')),
        reviewer TEXT, reason TEXT, created_at TEXT NOT NULL, reviewed_at TEXT,
        superseded_by TEXT, superseded_at TEXT, superseded_reviewer TEXT,
        superseded_reason TEXT, deleted_at TEXT, deletion_cause TEXT CHECK(deletion_cause IN
            ('manual', 'source_changed', 'source_removed', 'collection_reconfigured',
             'collection_removed'))
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS reviewed_claim_current
        ON reviewed_claims(workspace, claim_key) WHERE status='approved'""",
    """CREATE TABLE IF NOT EXISTS reviewed_claim_sources (
        claim_id TEXT NOT NULL REFERENCES reviewed_claims(id) ON DELETE CASCADE,
        collection_id TEXT NOT NULL, document_id TEXT NOT NULL,
        revision_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
        PRIMARY KEY(claim_id, chunk_id)
    )""",
)

# Callers supply an additional predicate and matching bound parameters.
TOMBSTONE_SQL = (
    "UPDATE reviewed_claims SET status='deleted', claim_key='', content='', "
    'reviewer=NULL, reason=NULL, superseded_by=NULL, superseded_at=NULL, '
    'superseded_reviewer=NULL, superseded_reason=NULL, deleted_at=?, deletion_cause=? '
    "WHERE status != 'deleted' AND "
)
