# Local chat memory relevance and credential hygiene

Local chat selects memories and tasks by relevance to the latest question. It does
not preload a fixed set of preferences or the most recently updated tasks, so an
unrelated coding rule does not appear in an everyday answer.

## Behavior

- Chat selects unpinned memories against the latest user message. Project scope
  remains current project plus global, or global only when no project is selected.
- Explicit pins remain included. MCP callers retain the existing standing-rule
  preload contract; the relevance policy is selected with `for_chat=True`.
- Keyword matches use meaningful whole words. Semantic matches require a cosine
  score of at least 0.50 and must be within 0.10 of the best match. When the best
  score is below 0.60, it must lead the runner-up by at least 0.05; ambiguous weak
  matches are omitted. These thresholds target the shipped BGE embedding model.
- Tasks must overlap the current topic in their title, objective or pending work.
  Relevance ranks first; recency breaks ties. Disabling memory or automatic
  retrieval also disables automatic task context.
- Saved context is JSON-encoded and described as background. Preferences should
  shape a relevant answer quietly; current user instructions take precedence.
  This improves separation but does not make prompt injection impossible.
- Retrieval traces include the conversation id and
  `chat-context-v1-relevance-2026-09-15` profile version.

## Credential handling

A shared redactor covers known provider token formats, private-key blocks, bearer
values and sufficiently credential-like labelled values. It runs on explicit
saves, edits, imports, extraction input/output, vault ingestion, embedding input,
memory reads/exports, keyword summaries, agent memory previews and chat task
context. Vault files themselves are not modified. Public client
ids and ordinary references to authentication are preserved.

Startup repairs matching legacy memory text, preserves ids/pins/non-secret
content, updates modification timestamps and invalidates affected embeddings.
Only remediation counts are logged. Original chat histories, source documents
and rollback backups are outside this cleanup; it is not forensic disk erasure.
Pattern detection is intentionally narrow and cannot detect every arbitrary
password or unknown credential format.

The extraction prompt substitutes only the conversation placeholder, so literal
JSON braces in the prompt text are never interpreted as format fields.

## Tests

Python tests cover the relevance and credential-redaction cases, memory editing,
import, instruction trust, wipe, chat privacy and persistence, the gateway and task
concurrency. A chat-context eval (`evals/eval_chat_context.py`) runs synthetic
memories against the local embedding model in isolated storage, and the MCP
retrieval eval keeps recall@5 above its regression floor and checks cross-project
isolation.

The chat corpus is a targeted regression gate, not a general answer-quality
benchmark. Task matching is lexical and can miss paraphrases. Very short
follow-ups rely on conversation history rather than refreshing old-topic memory.
Alternative embedding models need their own threshold evaluation.
