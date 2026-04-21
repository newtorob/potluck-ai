"""Local SQLite chat history storage.

Only active when privacy.chat_history_enabled is True in user settings.

TODO: Implement per guide §4.3 / data model:
  - ChatHistoryDB(db_path) — async SQLite via aiosqlite
  - create_conversation(title) -> str (conversation_id)
  - append_message(conversation_id, role, content)
  - list_conversations() -> list[dict]
  - get_messages(conversation_id) -> list[dict]
  - delete_conversation(conversation_id)
  - Schema: conversations(id, title, created_at), messages(id, conversation_id,
    role, content, created_at)
"""

from __future__ import annotations


class ChatHistoryDB:
    """Async SQLite-backed chat history store."""

    pass
