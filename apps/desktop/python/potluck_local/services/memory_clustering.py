"""Memory clustering - UMAP 2D projection for the cluster-map view.

Results are cached in-memory keyed on (project, n_active_memories).  The
cache is invalidated whenever a memory is saved, updated, or deleted.

UMAP parameters chosen:
  n_neighbors=15 - standard default; balances local vs global structure well
                     at the ~10-1000 memory scale we expect in v0.
  min_dist=0.1 - keeps clusters tight enough to be visually useful without
                     collapsing neighbours into a single point.
  metric='cosine' - matches the cosine-similarity we use everywhere else;
                     avoids re-normalising vectors for a different metric.
  n_components=2 - we need 2D coords for the map canvas, nothing more.

Fallback: if fewer than 8 memories have embeddings, UMAP produces unreliable
output (it needs at least n_neighbors+1 samples).  In that case we fall back
to PCA (via numpy SVD) which handles arbitrarily small n.  If even PCA is
impossible (<2 memories), we arrange points in a unit circle.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel

from potluck_local.services.memory_extraction import MemoryCategory

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_UMAP_MIN_SAMPLES = 8  # below this, fall back to PCA


# ---------------------------------------------------------------------------
# Response models (shared contract with frontend)
# ---------------------------------------------------------------------------


class ClusterPoint(BaseModel):
    id: str
    content_preview: str  # first 80 chars
    category: MemoryCategory
    project: str | None
    confidence: float
    x: float
    y: float


class ClusterResponse(BaseModel):
    points: list[ClusterPoint]
    generated_at: str  # ISO-8601 UTC
    n_memories: int
    degraded: bool = False  # True when embedder unavailable


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class ClusterCache:
    """Simple in-memory cache keyed on (project, n_active_memories).

    The n_active_memories component acts as a cheap content-change signal.
    Combined with explicit invalidation on write operations this is sufficient
    for v0 usage patterns.
    """

    def __init__(self) -> None:
        # key: project (str | None)  →  (signature: int, response: ClusterResponse)
        self._store: dict[str | None, tuple[int, ClusterResponse]] = {}

    async def _count_active(self, project: str | None) -> int:
        from potluck_local.services.memory_db import (
            get_memory_db_path,
            project_clause,
        )
        import aiosqlite

        db_path = get_memory_db_path()
        clause, params = project_clause(project)

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM memories WHERE is_active = 1 AND {clause}",
                params,
            )
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def get(self, project: str | None) -> ClusterResponse | None:
        entry = self._store.get(project)
        if entry is None:
            return None
        cached_sig, cached_resp = entry
        current_sig = await self._count_active(project)
        if current_sig != cached_sig:
            # Memory count changed - stale.
            del self._store[project]
            return None
        return cached_resp

    async def put(self, project: str | None, response: ClusterResponse) -> None:
        sig = await self._count_active(project)
        self._store[project] = (sig, response)

    async def invalidate(self, project: str | None = None) -> None:
        """Invalidate cache for a specific project scope.

        Pass project=None to clear the global-scope entry.
        Call with no argument (or None) for global; pass the project string to
        clear a named project entry.  To clear everything call invalidate_all().
        """
        self._store.pop(project, None)

    async def invalidate_all(self) -> None:
        """Clear the entire cache - call when a memory write affects all scopes."""
        self._store.clear()


_cluster_cache = ClusterCache()


def get_cluster_cache() -> ClusterCache:
    return _cluster_cache


# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------


def _pca_2d(matrix: np.ndarray) -> np.ndarray:
    """Project (n, d) matrix to (n, 2) using truncated SVD (no sklearn needed)."""
    # Centre the data
    centred = matrix - matrix.mean(axis=0)
    # SVD - we only need first 2 components
    try:
        _, _, vt = np.linalg.svd(centred, full_matrices=False)
        components = vt[:2]  # (2, d)
        projected = centred @ components.T  # (n, 2)
    except np.linalg.LinAlgError:
        # Degenerate matrix - fall back to circle
        projected = _circle_layout(len(matrix))
    return projected.astype(np.float32)


def _circle_layout(n: int) -> np.ndarray:
    """Arrange n points evenly on the unit circle."""
    angles = np.linspace(0, 2 * math.pi, n, endpoint=False)
    return np.column_stack([np.cos(angles), np.sin(angles)]).astype(np.float32)


def _umap_2d_sync(matrix: np.ndarray) -> np.ndarray:
    """Blocking UMAP projection - call via run_in_executor."""
    import umap  # type: ignore[import]

    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=15,
        min_dist=0.1,
        metric="cosine",
        random_state=42,
        low_memory=False,
    )
    return reducer.fit_transform(matrix).astype(np.float32)


async def _project_2d(matrix: np.ndarray) -> np.ndarray:
    """Choose UMAP or PCA based on sample count; run in executor."""
    n = len(matrix)
    if n < 2:
        return _circle_layout(n)

    loop = asyncio.get_event_loop()
    if n >= _UMAP_MIN_SAMPLES:
        try:
            return await loop.run_in_executor(None, _umap_2d_sync, matrix)
        except Exception as exc:
            logger.warning("umap_failed reason=%s, falling back to PCA", exc)

    # PCA fallback: pure numpy, no executor needed for this scale
    return await loop.run_in_executor(None, _pca_2d, matrix)


# ---------------------------------------------------------------------------
# Main cluster computation
# ---------------------------------------------------------------------------


async def compute_clusters(project: str | None) -> ClusterResponse:
    """Load embeddings, run 2D projection, return ClusterResponse.

    Checks the cache first; recomputes on miss.
    """
    cached = await _cluster_cache.get(project)
    if cached is not None:
        return cached

    from potluck_local.services.embeddings import get_embedder
    from potluck_local.services.memory_db import get_memory_db_path, load_all_embeddings
    from potluck_local.services.memory_retrieval import _row_to_memory
    import aiosqlite

    # Load embeddings for scoped active memories, filtered to the active model so
    # a mixed-model table (after an embedding-model switch) can't crash np.stack.
    pairs = await load_all_embeddings(project=project, model=get_embedder().MODEL_NAME)

    now_iso = datetime.now(timezone.utc).isoformat()

    if not pairs:
        resp = ClusterResponse(
            points=[],
            generated_at=now_iso,
            n_memories=0,
            degraded=True,
        )
        return resp

    memory_ids = [p[0] for p in pairs]
    matrix = np.stack([p[1] for p in pairs], axis=0)  # (n, DIM)

    # Fetch memory metadata for all IDs in one query
    db_path = get_memory_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in memory_ids)
        cursor = await db.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders})",  # noqa: S608
            memory_ids,
        )
        rows = {row["id"]: dict(row) for row in await cursor.fetchall()}

    # 2D projection
    coords = await _project_2d(matrix)  # (n, 2)

    points: list[ClusterPoint] = []
    for i, mid in enumerate(memory_ids):
        row = rows.get(mid)
        if row is None:
            continue
        mem = _row_to_memory(row)
        points.append(
            ClusterPoint(
                id=mem.id,
                content_preview=mem.content[:80],
                category=mem.category,
                project=mem.project,
                confidence=mem.confidence,
                x=float(coords[i, 0]),
                y=float(coords[i, 1]),
            )
        )

    resp = ClusterResponse(
        points=points,
        generated_at=now_iso,
        n_memories=len(points),
    )
    await _cluster_cache.put(project, resp)
    logger.info("cluster_computed project=%s n_memories=%d", project, len(points))
    return resp
