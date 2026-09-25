"""Usage dashboard store: the writer + aggregation reads over `usage_events`.

One row per completed inference request, written by each request path (see the
insertion points in routers/gateway.py, routers/peer.py, contributor/client.py,
and the frontend-driven POST /usage/record for in-app network chat).

PRIVACY INVARIANT (non-negotiable): metadata only. model, token counts, latency,
timestamp, scope, ok/fail. NEVER prompt or response content, and NEVER peer
identity on `served` rows. Everything here is computed and stored on-device in
the same sidecar SQLite file as memory; nothing is ever uploaded. This mirrors
the /agents args_summary metadata-only whitelist.

The table itself is created by memory_db.init_memory_db (the single schema
chokepoint); this module only reads and writes it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import anyio

from potluck_local.services.memory_db import get_memory_db_path
from potluck_local.utils.time import now_iso

logger = logging.getLogger(__name__)

# The four canonical routing scopes the dashboard reports. Every writer path
# normalizes into these so the consumed-vs-served split is coherent.
VALID_SCOPES = ("local", "my_machines", "circle", "pool")
VALID_DIRECTIONS = ("consumed", "served")

# A "session" is a burst of activity; consecutive events more than this many
# seconds apart start a new session. 30 minutes is the common analytics default.
_SESSION_GAP_SECONDS = 30 * 60


def _normalize_scope(scope: str | None) -> str:
    """Map the various scope spellings each request path uses into a canonical one.

    The gateway uses `mine` for household routing and `served_by="peer:..."`;
    the chat store sends `my_machines`/`circle`/`pool`/`local` directly. Anything
    unrecognized falls back to `local` rather than inventing a scope.
    """
    s = (scope or "").strip().lower()
    if s in VALID_SCOPES:
        return s
    if s in ("mine", "my machines", "household", "my_machine"):
        return "my_machines"
    if s in ("cooperative", "open", "open_pool", "network"):
        return "pool"
    return "local"


def _cutoff_iso(days: float) -> str:
    """ISO-8601 UTC timestamp `days` in the past, for `ts >= ?` window filters."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _percentile(sorted_values: list[int], pct: float) -> int:
    """Nearest-rank percentile over an already-sorted list. 0 when empty."""
    if not sorted_values:
        return 0
    # nearest-rank: ceil(pct/100 * N) clamped into range
    idx = max(0, min(len(sorted_values) - 1, int(round(pct / 100.0 * len(sorted_values))) - 1))
    return int(sorted_values[idx])


async def record_usage_event(
    *,
    direction: str,
    scope: str | None,
    model_slug: str | None,
    input_tokens: int = 0,
    cache_read_tokens: int = 0,
    output_tokens: int = 0,
    ttft_ms: int | None = None,
    duration_ms: int | None = None,
    ok: bool = True,
    error_class: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Insert one usage row. Failure-tolerant: a telemetry write must never break
    the request that produced it (this is called from streaming finalizers and a
    cross-process contributor writer, so it wraps everything and only logs)."""
    if direction not in VALID_DIRECTIONS:
        direction = "consumed"
    path = db_path or get_memory_db_path()
    # Stream finalizers run inside Starlette's cancelled AnyIO scope. Older
    # aiosqlite versions leak their non-daemon worker if connect is cancelled
    # before __aenter__ returns. Finish this small transaction, including close,
    # before propagating cancellation; never leave a fire-and-forget DB writer.
    async def write():
        try:
            async with aiosqlite.connect(path) as db:
                await db.execute("PRAGMA busy_timeout=5000")
                await db.execute(
                    """
                    INSERT INTO usage_events (
                      ts, direction, scope, model_slug,
                      input_tokens, cache_read_tokens, output_tokens,
                      ttft_ms, duration_ms, ok, error_class
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now_iso(),
                        direction,
                        _normalize_scope(scope),
                        model_slug,
                        max(0, int(input_tokens or 0)),
                        max(0, int(cache_read_tokens or 0)),
                        max(0, int(output_tokens or 0)),
                        int(ttft_ms) if ttft_ms is not None else None,
                        int(duration_ms) if duration_ms is not None else None,
                        1 if ok else 0,
                        error_class,
                    ),
                )
                await db.commit()
        except Exception:
            logger.exception("usage_event_write_failed direction=%s scope=%s", direction, scope)

    with anyio.CancelScope(shield=True):
        worker = asyncio.create_task(write())
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            worker.result()
            raise


async def summary(window_days: float = 30.0, db_path: Path | None = None) -> dict:
    """Headline stats over the window: requests, tokens, sessions, active days,
    streak, success rate, p95 latency, avg TTFT. Consumed-side only (the "my
    usage" hero), since served rows are work-for-others, not personal usage."""
    path = db_path or get_memory_db_path()
    cutoff = _cutoff_iso(window_days)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
              COUNT(*) AS requests,
              COALESCE(SUM(input_tokens), 0) AS input_tokens,
              COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
              COALESCE(SUM(output_tokens), 0) AS output_tokens,
              COALESCE(SUM(ok), 0) AS ok_count,
              COALESCE(AVG(ttft_ms), 0) AS avg_ttft_ms
            FROM usage_events
            WHERE direction = 'consumed' AND ts >= ?
            """,
            (cutoff,),
        )
        agg = dict(await cur.fetchone())

        # Durations + timestamps for p95 and session/streak logic (small data).
        cur = await db.execute(
            "SELECT ts, duration_ms FROM usage_events "
            "WHERE direction = 'consumed' AND ts >= ? ORDER BY ts ASC",
            (cutoff,),
        )
        rows = await cur.fetchall()

    durations = sorted(int(r["duration_ms"]) for r in rows if r["duration_ms"] is not None)
    p95 = _percentile(durations, 95)

    # Sessions: clusters of events < 30 min apart. Active days + streak from dates.
    timestamps: list[datetime] = []
    for r in rows:
        try:
            timestamps.append(datetime.fromisoformat(r["ts"]))
        except (ValueError, TypeError):
            continue
    sessions = 0
    last_dt: datetime | None = None
    for dt in timestamps:
        if last_dt is None or (dt - last_dt).total_seconds() > _SESSION_GAP_SECONDS:
            sessions += 1
        last_dt = dt

    active_dates = {dt.date() for dt in timestamps}
    active_days = len(active_dates)
    streak = 0
    today = datetime.now(timezone.utc).date()
    # A streak counts back from today; if there's nothing today yet, start from
    # yesterday so an active-yesterday streak isn't reported as broken mid-morning.
    cursor_day = today if today in active_dates else today - timedelta(days=1)
    while cursor_day in active_dates:
        streak += 1
        cursor_day = cursor_day - timedelta(days=1)

    requests = int(agg["requests"])
    return {
        "requests": requests,
        "input_tokens": int(agg["input_tokens"]),
        "cache_read_tokens": int(agg["cache_read_tokens"]),
        "output_tokens": int(agg["output_tokens"]),
        "total_tokens": int(agg["input_tokens"]) + int(agg["output_tokens"]),
        "sessions": sessions,
        "active_days": active_days,
        "active_streak": streak,
        "success_rate": (int(agg["ok_count"]) / requests) if requests else 1.0,
        "p95_latency_ms": p95,
        "avg_ttft_ms": int(agg["avg_ttft_ms"] or 0),
    }


async def performance_summary(db_path: Path | None = None) -> dict:
    """Bounded aggregate over recorded consumed requests in the last 24 hours."""
    async with aiosqlite.connect(db_path or get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT COUNT(*) AS requests, SUM(output_tokens) AS output_tokens, "
            "AVG(ttft_ms) AS avg_ttft_ms, AVG(duration_ms) AS avg_duration_ms, "
            "AVG(ok) AS success_rate FROM usage_events WHERE direction = 'consumed' AND ts >= ?",
            (_cutoff_iso(1),))
        row = dict(await cursor.fetchone())
    return {'windowHours': 24, 'requests': row['requests'], 'reportedOutputTokens': row['output_tokens'] or 0,
            'avgTtftMs': row['avg_ttft_ms'], 'avgDurationMs': row['avg_duration_ms'], 'successRate': row['success_rate']}


async def activity(range_days: int = 365, db_path: Path | None = None) -> list[dict]:
    """Per-day [{date, tokens, requests}] for the contribution heatmap (consumed)."""
    path = db_path or get_memory_db_path()
    cutoff = _cutoff_iso(range_days)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
              substr(ts, 1, 10) AS date,
              COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
              COUNT(*) AS requests
            FROM usage_events
            WHERE direction = 'consumed' AND ts >= ?
            GROUP BY substr(ts, 1, 10)
            ORDER BY date ASC
            """,
            (cutoff,),
        )
        rows = await cur.fetchall()
    return [{"date": r["date"], "tokens": int(r["tokens"]), "requests": int(r["requests"])} for r in rows]


async def top_models(limit: int = 8, db_path: Path | None = None) -> list[dict]:
    """Top models by total tokens (consumed), with request counts."""
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
              COALESCE(model_slug, 'unknown') AS model_slug,
              COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
              COUNT(*) AS requests
            FROM usage_events
            WHERE direction = 'consumed'
            GROUP BY model_slug
            ORDER BY tokens DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    return [
        {"model_slug": r["model_slug"], "tokens": int(r["tokens"]), "requests": int(r["requests"])}
        for r in rows
    ]


async def pace(db_path: Path | None = None) -> dict:
    """Recent-throughput card: last hour, last 24h, week-over-week change,
    cache-hit rate, avg TTFT. Consumed side."""
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row

        async def _tokens_since(cutoff: str) -> int:
            cur = await db.execute(
                "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS t "
                "FROM usage_events WHERE direction = 'consumed' AND ts >= ?",
                (cutoff,),
            )
            return int((await cur.fetchone())["t"])

        async def _tokens_between(lo: str, hi: str) -> int:
            cur = await db.execute(
                "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS t "
                "FROM usage_events WHERE direction = 'consumed' AND ts >= ? AND ts < ?",
                (lo, hi),
            )
            return int((await cur.fetchone())["t"])

        last_hour = await _tokens_since(_cutoff_iso(1 / 24.0))
        last_24h = await _tokens_since(_cutoff_iso(1.0))
        this_week = await _tokens_since(_cutoff_iso(7.0))
        prev_week = await _tokens_between(_cutoff_iso(14.0), _cutoff_iso(7.0))

        cur = await db.execute(
            """
            SELECT
              COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
              COALESCE(SUM(input_tokens), 0) AS input_tok,
              COALESCE(AVG(ttft_ms), 0) AS avg_ttft
            FROM usage_events
            WHERE direction = 'consumed' AND ts >= ?
            """,
            (_cutoff_iso(30.0),),
        )
        c = dict(await cur.fetchone())

    wow = None
    if prev_week > 0:
        wow = round((this_week - prev_week) / prev_week * 100.0, 1)
    # cache-hit rate is 0 until prefix caching is tracked (v1 ships without it).
    denom = int(c["cache_read"]) + int(c["input_tok"])
    cache_hit_rate = (int(c["cache_read"]) / denom) if denom else 0.0
    return {
        "last_hour_tokens": last_hour,
        "last_24h_tokens": last_24h,
        "wow_change_pct": wow,
        "cache_hit_rate": cache_hit_rate,
        "avg_ttft_ms": int(c["avg_ttft"] or 0),
    }


async def split(window_days: float = 30.0, db_path: Path | None = None) -> dict:
    """The Potluck-specific symmetric view: tokens you CONSUMED vs tokens you
    SERVED for others, each broken down by scope. This is the half no cloud or
    CLI competitor can render, so the UI leads with it."""
    path = db_path or get_memory_db_path()
    cutoff = _cutoff_iso(window_days)

    def _empty() -> dict:
        return {s: {"tokens": 0, "requests": 0} for s in VALID_SCOPES}

    consumed = _empty()
    served = _empty()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
              direction, scope,
              COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
              COUNT(*) AS requests
            FROM usage_events
            WHERE ts >= ?
            GROUP BY direction, scope
            """,
            (cutoff,),
        )
        rows = await cur.fetchall()
    for r in rows:
        scope = _normalize_scope(r["scope"])
        bucket = consumed if r["direction"] == "consumed" else served
        bucket[scope] = {"tokens": int(r["tokens"]), "requests": int(r["requests"])}

    return {
        "window_days": window_days,
        "consumed": consumed,
        "served": served,
        "consumed_total": sum(v["tokens"] for v in consumed.values()),
        "served_total": sum(v["tokens"] for v in served.values()),
    }
