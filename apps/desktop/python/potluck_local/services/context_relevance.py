"""Conservative lexical relevance for automatic local-chat context.

The latest user turn owns the topic. Earlier turns and generated answers must
not keep an old project alive after the user asks an unrelated question.
Semantic memory retrieval remains a separate, thresholded candidate source.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from potluck_local.services.memory_safety import redact_credentials

if TYPE_CHECKING:
    from potluck_local.services.task_state import TaskState

_STOPWORDS = frozenset("""
a an the is are was were be been being have has had do does did will would could
should may might shall can need ought used to of in for on with at by from as
into through during before after above below between out off over under again
further then once here there when where why how all both each few more most other
some such no nor not only own same so than too very just now i me my we our you
your he she it they them his her its this that what which who whom these those am
but if or because about up and don also help improve please tell give want like
know better use using thing things question answer today make suggest recommend
ideas thanks thank hello hi lets let user users prefers prefer always never
""".split())


def topic_terms(text: str) -> set[str]:
    words = re.findall(r"[^\W_]+", redact_credentials(text).casefold())
    return {word for word in words if len(word) > 2 and word not in _STOPWORDS
            and word not in {"redacted", "credential"}}


def latest_user_query(messages: list[dict]) -> str:
    return next((str(m.get("content", "")) for m in reversed(messages)
                 if m.get("role") == "user"), "")


def lexical_relevance(query_terms: set[str], text: str) -> float:
    if not query_terms:
        return 0.0
    return len(query_terms & topic_terms(text)) / len(query_terms)


def relevant_tasks(tasks: list[TaskState], query: str, limit: int = 3) -> list[TaskState]:
    """Rank active, already project-scoped tasks; recency only breaks ties.

    Match title/objective/pending work, not logs or old error messages. An unrelated
    newly updated task should never enter the prompt just because it was touched.
    """
    terms = topic_terms(query)
    ranked = []
    for task in tasks:
        if task.status != "active":
            continue
        score = 2 * lexical_relevance(terms, task.task_name)
        score += lexical_relevance(terms, " ".join([task.objective, *task.pending_steps]))
        if score > 0:
            ranked.append((score, task.updated_at, task.id, task))
    ranked.sort(key=lambda row: row[:3], reverse=True)
    return [row[3] for row in ranked[:limit]]
