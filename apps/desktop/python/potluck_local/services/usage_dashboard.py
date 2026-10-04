"""Consistent, bounded metadata snapshots for the terminal usage dashboard.

Rates are successful reported output / wall time (including prefill), never
live decode speed. Reads stay on this computer; household probes are opt-in.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import psutil

from potluck_local.services.memory_db import get_memory_db_path


_STATS = """
    COUNT(*) AS requests,
    COALESCE(SUM(input_tokens), 0) AS inputTokens,
    COALESCE(SUM(output_tokens), 0) AS outputTokens,
    COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failures,
    AVG(CASE WHEN ok = 1 AND ttft_ms >= 0 THEN ttft_ms END) AS avgTtftMs,
    AVG(CASE WHEN ok = 1 AND duration_ms >= 0 THEN duration_ms END) AS avgDurationMs,
    SUM(CASE WHEN ok = 1 AND duration_ms > 0 AND output_tokens > 0 THEN output_tokens END)
        * 1000.0 / SUM(CASE WHEN ok = 1 AND duration_ms > 0 AND output_tokens > 0
                          THEN duration_ms END) AS endToEndTokensPerSecond,
    COUNT(CASE WHEN ok = 1 AND duration_ms > 0 AND output_tokens > 0 THEN 1 END) AS rateSamples
"""


async def usage_snapshot(window: int = 7, direction: str = "consumed", *,
                         db_path: Path | None = None, now: datetime | None = None) -> dict:
    if window not in (1, 7, 30, 90) or direction not in ("consumed", "served"):
        raise ValueError("Unsupported usage window or direction")
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window)
    params = (direction, cutoff.isoformat(), now.isoformat())
    where = "direction = ? AND ts >= ? AND ts <= ?"
    # One read transaction: totals, charts and rows describe the same database
    # snapshot even when inference completions arrive during the queries.
    async with aiosqlite.connect(db_path or get_memory_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("PRAGMA busy_timeout=1000")
        await db.execute("BEGIN")

        async def rows(sql, values=params):
            async with db.execute(sql, values) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

        totals = (await rows(f"SELECT {_STATS} FROM usage_events WHERE {where}"))[0]
        latency_where = where + " AND ok = 1 AND duration_ms >= 0"
        count = (await rows(f"SELECT COUNT(*) AS n FROM usage_events WHERE {latency_where}"))[0]["n"]
        # Nearest-rank percentile, sorted in SQLite rather than loading history.
        percentile = await rows(f"SELECT duration_ms FROM usage_events WHERE {latency_where} "
                                "ORDER BY duration_ms LIMIT 1 OFFSET ?",
                                (*params, max(0, (95 * count + 99) // 100 - 1)))
        totals["p95DurationMs"] = percentile[0]["duration_ms"] if percentile else None
        models = await rows(f"SELECT model_slug AS model, {_STATS} FROM usage_events WHERE {where} "
                            "GROUP BY model_slug ORDER BY requests DESC, model_slug LIMIT 20")
        routes = await rows(f"SELECT scope, COUNT(*) AS requests, SUM(output_tokens) AS outputTokens "
                            f"FROM usage_events WHERE {where} GROUP BY scope ORDER BY requests DESC")
        recent = await rows(f"SELECT id, ts, model_slug AS model, scope, input_tokens AS inputTokens, "
                            "output_tokens AS outputTokens, ttft_ms AS ttftMs, duration_ms AS durationMs, ok "
                            f"FROM usage_events WHERE {where} ORDER BY ts DESC, id DESC LIMIT 100")
        unit = "hour" if window == 1 else "day"
        length = 13 if unit == "hour" else 10
        activity = await rows(f"SELECT substr(ts, 1, {length}) AS bucket, COUNT(*) AS requests, "
                              f"SUM(output_tokens) AS outputTokens FROM usage_events WHERE {where} "
                              "GROUP BY bucket ORDER BY bucket")
    buckets = {row["bucket"]: row for row in activity}
    tick = cutoff.replace(minute=0, second=0, microsecond=0)
    if unit == "day":
        tick = tick.replace(hour=0)
    step = timedelta(hours=1) if unit == "hour" else timedelta(days=1)
    filled = []
    while tick <= now:
        key = tick.isoformat()[:length]
        filled.append(buckets.get(key, {"bucket": key, "requests": 0, "outputTokens": 0}))
        tick += step
    return {"schemaVersion": 1, "sampledAt": now.isoformat(), "windowDays": window,
            "direction": direction, "since": cutoff.isoformat(), "bucketUnit": unit,
            "totals": totals, "activity": filled, "models": models, "routes": routes,
            "recent": recent}


async def dashboard(window: int, direction: str, household: bool) -> dict:
    from potluck_local.services.performance_status import performance_status

    result = await usage_snapshot(window, direction)
    # Runtime or optional household failures do not erase recorded usage.
    try:
        performance = await performance_status(include_household=household)
        result["performance"] = {key: performance[key] for key in ("schemaVersion", "sampledAt", "local", "household")}
        result["memoryTotalGiB"] = round(psutil.virtual_memory().total / 1024 ** 3, 2)
    except Exception:
        result["performance"] = None
        result["memoryTotalGiB"] = None
    try:
        # Some released runtimes intentionally omit managed split inference.
        # Optional telemetry must not make the usage endpoint unavailable.
        from potluck_local.services.split_manager import get_split_manager
        split = get_split_manager().status()
        session = split.get("session") or {}
        # Never include session IDs, worker names/addresses, errors or paths.
        result["split"] = {"state": split["state"], "workerEnabled": split["workerEnabled"],
                           "workerBusy": split["workerBusy"], "workers": len(session.get("workers", [])),
                           "model": (session.get("model") or {}).get("id")}
    except Exception:
        result["split"] = None
    return result
