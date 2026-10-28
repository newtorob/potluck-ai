"""Current source evidence, isolated from personal memories and agent retrieval."""

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
)
