"""Local-chat memory precision/recall gate using the installed embedding model.

Synthetic corpus, isolated SQLite/config, no chat model or external services.
Run: HF_HUB_OFFLINE=1 .venv/bin/python -m evals.eval_chat_context
The gate requires every expected memory and forbids unrelated context. This is
a targeted regression corpus, not an estimate of general answer quality.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

CORPUS = [
    ("pnpm", "Use pnpm instead of npm for JavaScript package management.", "instruction", None),
    ("ci", "Do not enable GitHub Actions CI because its billing costs money.", "preference", None),
    ("sleep", "I sleep poorly after caffeine, so I avoid coffee after two in the afternoon.", "preference", None),
    ("dinner", "I eat vegetarian food and especially enjoy chickpea curry.", "preference", None),
    ("hardware", "My laptop has 16 GB of unified memory and an Apple M2 chip.", "fact", None),
    ("travel", "For vacations I prefer quiet coastal towns to crowded cities.", "preference", None),
    ("python", "Run pytest for the Python backend unit tests.", "instruction", "potluck"),
    ("downloads", "Potluck model downloads must preserve partial files after interruptions.", "fact", "potluck"),
    ("other", "The other project's Python tests require its private database.", "fact", "other"),
]
CASES = [
    ("sleep", "How can I improve my sleep?", None, {"sleep"}, {"sleep"}),
    ("dinner", "What should I make for dinner?", None, {"dinner"}, {"dinner"}),
    ("packages", "Which package manager should I use for JavaScript?", None, {"pnpm"}, {"pnpm"}),
    ("ci", "Should I enable GitHub Actions CI?", None, {"ci"}, {"ci"}),
    ("hardware", "How much RAM does my laptop have?", None, {"hardware"}, {"hardware"}),
    ("travel", "Suggest a vacation destination", None, {"travel"}, {"travel"}),
    ("python", "How do I run Python unit tests?", "potluck", {"python"}, {"python", "ci"}),
    ("downloads", "Can an interrupted model download resume?", "potluck", {"downloads"}, {"downloads"}),
    ("unrelated", "Explain how tides work", None, set(), set()),
    ("greeting", "Hello", None, set(), set()),
    ("cross-project", "How do I run Python unit tests?", None, set(), {"ci"}),
]


async def evaluate():
    from potluck_local.services.embeddings import get_embedder
    from potluck_local.services.memory_db import init_memory_db, save_embedding
    from potluck_local.services.memory_extraction import MemoryExtractor
    from potluck_local.services.memory_retrieval import MemoryRetriever

    await init_memory_db()
    embedder = get_embedder()
    vectors = await embedder.embed_many([entry[1] for entry in CORPUS])
    if vectors is None:
        print("Embedding model unavailable; real semantic evaluation did not run.")
        return 2
    ids = {}
    with patch("potluck_local.services.embeddings.schedule_embed_memory"):
        for (name, text, category, project), vec in zip(CORPUS, vectors):
            mem = await MemoryExtractor().extract_explicit(text, category, project=project)
            ids[mem.id] = name
            await save_embedding(mem.id, vec, embedder.MODEL_NAME)
    failures = 0
    for name, query, project, required, allowed in CASES:
        result = await MemoryRetriever().get_relevant_memories(
            [{"role": "user", "content": query}], project=project, for_chat=True, max_memories=6,
        )
        found = {ids[mem.id] for mem in result}
        passed = required <= found <= allowed
        failures += not passed
        print(json.dumps({"case": name, "passed": passed, "selected": sorted(found),
                          "missing": sorted(required - found), "unrelated": sorted(found - allowed)}))
    print(f"{len(CASES) - failures}/{len(CASES)} local-chat context cases passed")
    return int(failures > 0)


def main():
    with tempfile.TemporaryDirectory(prefix="potluck-chat-context-eval-") as tmp:
        with patch.dict(os.environ, {"POTLUCK_MEMORY_DB_PATH": str(Path(tmp) / "memory.db"),
                                     "POTLUCK_DATA_DIR": str(Path(tmp) / "data")}):
            return asyncio.run(evaluate())


if __name__ == "__main__":
    raise SystemExit(main())
