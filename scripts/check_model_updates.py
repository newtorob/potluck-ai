#!/usr/bin/env python3
"""Model-catalog update checker + verified current-gen catalog rebuild.

Maintainer tool. Compares a curated, hand-edited LINEUP spec (the models we
*want* to ship) against the shipped catalog (apps/desktop/python/potluck_local/
data/models.json), resolves REAL metadata for every lineup entry straight from
the Hugging Face Hub API (sha256, size, last-modified date -- no downloading
the actual GGUF weights), and reports what would change.

Usage:
    apps/desktop/python/.venv/bin/python scripts/check_model_updates.py
    apps/desktop/python/.venv/bin/python scripts/check_model_updates.py --json
    apps/desktop/python/.venv/bin/python scripts/check_model_updates.py --write

Modes:
    (default)  Human-readable report: per family/tier UPGRADE / NEW /
               UNCHANGED / REMOVED, plus a DRIFT section flagging any family
               where HF already has a newer generation than LINEUP covers.
    --json     Same report, machine-readable.
    --write    Also resolve every LINEUP entry's live metadata and emit a
               refreshed catalog to models.json.proposed (schema_version 2).
               Never touches the shipped models.json.

Network: pure public HF Hub API (HfApi + the unauthenticated
`/api/models/{repo}?expand[]=gguf` endpoint for GGUF header metadata,
i.e. context length, without pulling the multi-GB file). No tokens.
Every remote call is wrapped so a single flaky/renamed/gated repo just gets
skipped and reported -- the run never crashes.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.utils import HfHubHTTPError

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = (
    REPO_ROOT
    / "apps/desktop/python/potluck_local/data/models.json"
)
PROPOSED_PATH = CATALOG_PATH.with_name("models.json.proposed")

SCHEMA_VERSION = 2

HF_GGUF_EXPAND_URL = "https://huggingface.co/api/models/{repo}?expand[]=gguf"


# ---------------------------------------------------------------------------
# CURATED LINEUP SPEC -- hand-edit this to bump generations.
#
# Each entry names the exact HF repo + GGUF filename we ship. Sizes/hashes
# below are NOT trusted -- they're re-resolved live from the Hub every run.
# Quant choice: Q4_K_M is the default (matches the shipped catalog's
# convention); Q5_K_M only where the shipped predecessor already used it
# (llama-3.1-8b, to avoid a quality regression on an unchanged model).
#
# `base_repo` is the (often gated) non-GGUF HF repo used to best-effort
# resolve num_hidden_layers from config.json. When that repo is gated/
# unreachable, DOCUMENTED_LAYERS below (sourced from the model's published
# card/technical report, not fabricated) is the fallback, then
# `fallback_num_layers` as a last resort.
# ---------------------------------------------------------------------------


@dataclass
class LineupEntry:
    slug: str
    family: str  # lowercase family slug: qwen | llama | gemma | phi | deepseek | mistral
    tier: str  # "small" | "medium" | "large"
    recommended_tier: str
    name: str
    description: str
    repo: str  # HF repo hosting the GGUF file
    filename: str  # exact gguf filename inside that repo
    min_ram_gb: int
    min_vram_gb: int
    is_featured: bool = False
    base_repo: str | None = None  # non-GGUF repo for config.json layer lookup
    fallback_context_window: int = 8192
    fallback_num_layers: int = 32


LINEUP: list[LineupEntry] = [
    # ---- small (fits 8GB RAM / no GPU) -------------------------------
    LineupEntry(
        slug="qwen3-1.7b-q4",
        family="qwen",
        tier="small",
        recommended_tier="small",
        name="Qwen3 1.7B",
        description="Very small and fast. Great for autocomplete, quick classification, and low-power machines.",
        repo="unsloth/Qwen3-1.7B-GGUF",
        filename="Qwen3-1.7B-Q4_K_M.gguf",
        min_ram_gb=4,
        min_vram_gb=2,
        base_repo="unsloth/Qwen3-1.7B",
        fallback_context_window=32768,
        fallback_num_layers=28,
    ),
    LineupEntry(
        slug="qwen3-4b-instruct-2507-q4",
        family="qwen",
        tier="small",
        recommended_tier="small",
        name="Qwen3 4B Instruct",
        description="Fast, lightweight, and current-gen. Great default for quick questions, writing help, and everyday tasks.",
        repo="unsloth/Qwen3-4B-Instruct-2507-GGUF",
        filename="Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        min_ram_gb=8,
        min_vram_gb=4,
        is_featured=True,
        base_repo="Qwen/Qwen3-4B-Instruct-2507",
        fallback_context_window=32768,
        fallback_num_layers=36,
    ),
    LineupEntry(
        slug="llama-3.2-3b-instruct-q4",
        family="llama",
        tier="small",
        recommended_tier="small",
        name="Llama 3.2 3B Instruct",
        description="Compact Llama with a large context window. Good for everyday tasks and long inputs.",
        repo="bartowski/Llama-3.2-3B-Instruct-GGUF",
        filename="Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        min_ram_gb=8,
        min_vram_gb=3,
        base_repo="meta-llama/Llama-3.2-3B-Instruct",  # gated -> DOCUMENTED_LAYERS fallback
        fallback_context_window=131072,
        fallback_num_layers=28,
    ),
    LineupEntry(
        slug="gemma-3-4b-it-q4",
        family="gemma",
        tier="small",
        recommended_tier="small",
        name="Gemma 3 4B Instruct",
        description="Small, capable Google model with a long context window. A solid current-gen lightweight default.",
        repo="bartowski/google_gemma-3-4b-it-GGUF",
        filename="google_gemma-3-4b-it-Q4_K_M.gguf",
        min_ram_gb=8,
        min_vram_gb=4,
        base_repo="google/gemma-3-4b-it",  # gated -> DOCUMENTED_LAYERS fallback
        fallback_context_window=131072,
        fallback_num_layers=34,
    ),
    LineupEntry(
        slug="phi-4-mini-instruct-q4",
        family="phi",
        tier="small",
        recommended_tier="small",
        name="Phi-4 Mini Instruct",
        description="Compact Microsoft model with a large context window and strong reasoning for its size.",
        repo="bartowski/microsoft_Phi-4-mini-instruct-GGUF",
        filename="microsoft_Phi-4-mini-instruct-Q4_K_M.gguf",
        min_ram_gb=8,
        min_vram_gb=4,
        base_repo="microsoft/Phi-4-mini-instruct",
        fallback_context_window=131072,
        fallback_num_layers=32,
    ),
    # ---- medium (16GB RAM or 6GB+ VRAM) -------------------------------
    LineupEntry(
        slug="qwen3-8b-q4",
        family="qwen",
        tier="medium",
        recommended_tier="medium",
        name="Qwen3 8B",
        description="Strong current-gen all-rounder for coding, analysis, and longer conversations.",
        repo="Qwen/Qwen3-8B-GGUF",
        filename="Qwen3-8B-Q4_K_M.gguf",
        min_ram_gb=16,
        min_vram_gb=6,
        is_featured=True,
        base_repo="Qwen/Qwen3-8B",
        fallback_context_window=32768,
        fallback_num_layers=36,
    ),
    LineupEntry(
        slug="llama-3.1-8b-instruct-q5",
        family="llama",
        tier="medium",
        recommended_tier="medium",
        name="Llama 3.1 8B Instruct",
        description="Balanced model that handles coding, analysis, and complex multi-turn conversations well.",
        repo="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        filename="Meta-Llama-3.1-8B-Instruct-Q5_K_M.gguf",
        min_ram_gb=16,
        min_vram_gb=6,
        base_repo="meta-llama/Llama-3.1-8B-Instruct",  # gated -> DOCUMENTED_LAYERS fallback
        fallback_context_window=131072,
        fallback_num_layers=32,
    ),
    LineupEntry(
        slug="deepseek-r1-distill-llama-8b-q4",
        family="deepseek",
        tier="medium",
        recommended_tier="medium",
        name="DeepSeek R1 Distill Llama 8B",
        description="Reasoning-distilled model. Shows its work step by step; strong at math and multi-step problems.",
        repo="bartowski/DeepSeek-R1-Distill-Llama-8B-GGUF",
        filename="DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf",
        min_ram_gb=16,
        min_vram_gb=6,
        base_repo="deepseek-ai/DeepSeek-R1-Distill-Llama-8B",
        fallback_context_window=131072,
        fallback_num_layers=32,
    ),
    LineupEntry(
        slug="gemma-3-12b-it-q4",
        family="gemma",
        tier="medium",
        recommended_tier="medium",
        name="Gemma 3 12B Instruct",
        description="Larger Gemma with a long context window. Handles longer documents and denser reasoning well.",
        repo="bartowski/google_gemma-3-12b-it-GGUF",
        filename="google_gemma-3-12b-it-Q4_K_M.gguf",
        min_ram_gb=16,
        min_vram_gb=10,
        base_repo="google/gemma-3-12b-it",  # gated -> DOCUMENTED_LAYERS fallback
        fallback_context_window=131072,
        fallback_num_layers=48,
    ),
    LineupEntry(
        slug="phi-4-q4",
        family="phi",
        tier="medium",
        recommended_tier="medium",
        name="Phi-4",
        description="Microsoft's full-size reasoning-focused model. Strong at math and structured problem solving.",
        repo="unsloth/phi-4-GGUF",
        filename="phi-4-Q4_K_M.gguf",
        min_ram_gb=20,
        min_vram_gb=12,
        base_repo="microsoft/phi-4",
        fallback_context_window=16384,
        fallback_num_layers=40,
    ),
    LineupEntry(
        slug="ministral-8b-instruct-2410-q4",
        family="mistral",
        tier="medium",
        recommended_tier="medium",
        name="Ministral 8B Instruct",
        description="Mistral's efficient 8B model. Fast, and strong for its size on everyday assistant tasks.",
        repo="bartowski/Ministral-8B-Instruct-2410-GGUF",
        filename="Ministral-8B-Instruct-2410-Q4_K_M.gguf",
        min_ram_gb=16,
        min_vram_gb=6,
        base_repo="mistralai/Ministral-8B-Instruct-2410",
        fallback_context_window=32768,
        fallback_num_layers=36,
    ),
    # ---- large (32GB RAM or 16GB+ VRAM) -------------------------------
    LineupEntry(
        slug="qwen3-32b-q4",
        family="qwen",
        tier="large",
        recommended_tier="large",
        name="Qwen3 32B",
        description="Maximum-capability current-gen model for deep research, long documents, and demanding reasoning tasks.",
        repo="Qwen/Qwen3-32B-GGUF",
        filename="Qwen3-32B-Q4_K_M.gguf",
        min_ram_gb=32,
        min_vram_gb=20,
        is_featured=True,
        base_repo="Qwen/Qwen3-32B",
        fallback_context_window=32768,
        fallback_num_layers=64,
    ),
    LineupEntry(
        slug="deepseek-r1-distill-qwen-32b-q4",
        family="deepseek",
        tier="large",
        recommended_tier="large",
        name="DeepSeek R1 Distill Qwen 32B",
        description="Large reasoning-distilled model. Best-in-class local reasoning for math, code, and multi-step logic.",
        repo="bartowski/DeepSeek-R1-Distill-Qwen-32B-GGUF",
        filename="DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf",
        min_ram_gb=32,
        min_vram_gb=20,
        base_repo="deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
        fallback_context_window=131072,
        fallback_num_layers=64,
    ),
    LineupEntry(
        slug="gemma-3-27b-it-q4",
        family="gemma",
        tier="large",
        recommended_tier="large",
        name="Gemma 3 27B Instruct",
        description="Google's largest Gemma 3. Strong general reasoning with a long context window, more accessible than the 32B pair.",
        repo="bartowski/google_gemma-3-27b-it-GGUF",
        filename="google_gemma-3-27b-it-Q4_K_M.gguf",
        min_ram_gb=28,
        min_vram_gb=18,
        base_repo="google/gemma-3-27b-it",  # gated -> DOCUMENTED_LAYERS fallback
        fallback_context_window=131072,
        fallback_num_layers=62,
    ),
]

# Public, documented layer counts (from each model's official card / technical
# report) used ONLY as a fallback when `base_repo`'s config.json 401s because
# the repo is gate-walled (Llama, Gemma). Never fabricated -- these are the
# same numbers Meta/Google publish; just not fetchable anonymously via the API.
DOCUMENTED_LAYERS: dict[str, int] = {
    "meta-llama/Llama-3.2-3B-Instruct": 28,  # Meta Llama 3.2 model card
    "meta-llama/Llama-3.1-8B-Instruct": 32,  # Meta Llama 3.1 model card
    "google/gemma-3-4b-it": 34,  # Gemma 3 technical report, Table 1
    "google/gemma-3-12b-it": 48,  # Gemma 3 technical report, Table 1
    "google/gemma-3-27b-it": 62,  # Gemma 3 technical report, Table 1
}

# Drift-scan: one curated "what would the NEXT generation search like" query
# per family, paired with a normalized match token. A hit here doesn't
# auto-replace LINEUP (that's still a human call -- naming, quant
# availability, and RAM budget all need judgment) but it tells a maintainer
# the spec above is due for a bump.
#
# HF search is fuzzy (substring/fine-tune noise: "Phi-3_5" surfaces on a
# "Phi-5" query, an unrelated "R2MU-DeepSeek-R1..." finetune surfaces on
# "DeepSeek-R2"). `token` is checked as a contiguous alnum-normalized
# substring of the result's repo id so real next-gen releases are kept and
# fuzzy noise is dropped.
DRIFT_QUERIES: dict[str, tuple[str, str]] = {
    "qwen": ("Qwen3.5 Instruct GGUF", "qwen35"),
    "llama": ("Llama-4 Instruct GGUF", "llama4"),
    "gemma": ("gemma-4 it GGUF", "gemma4"),
    "phi": ("Phi-5 GGUF", "phi5"),
    "deepseek": ("DeepSeek-R2 GGUF", "deepseekr2"),
    "mistral": ("Mistral Large 3 GGUF", "mistrallarge3"),
}

# A drift hit only counts if it comes from a base-model org or a top-tier GGUF
# requantizer. This is what separates a real next-gen release from community
# noise: a token match alone surfaces junk like
# "mradermacher/Qwen3.5-...-HERETIC-UNCENSORED" or "RichardErkhov/...Phi-5B-Test"
# on families that have NO official next generation yet. Owners here either
# publish the original weights (qwen, google, meta-llama, microsoft,
# mistralai, deepseek-ai) or are the widely-trusted GGUF mirrors we already
# source LINEUP from (unsloth, bartowski, ggml-org, lmstudio-community).
TRUSTED_UPLOADERS: frozenset[str] = frozenset(
    {
        "qwen",
        "google",
        "meta-llama",
        "microsoft",
        "mistralai",
        "deepseek-ai",
        "unsloth",
        "bartowski",
        "ggml-org",
        "lmstudio-community",
    }
)


def _normalize(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())

# family+tier the old catalog's coder-specialized entry belongs to, called out
# explicitly since it has no direct replacement in LINEUP (see report).
OLD_CODER_SLUG = "qwen-2.5-coder-7b-instruct-q4"

# ---------------------------------------------------------------------------
# Old-catalog family classification (models.json v1 has no `family` field).
# ---------------------------------------------------------------------------

OLD_FAMILY: dict[str, str] = {
    "qwen-2.5-3b-instruct-q4": "qwen",
    "llama-3.1-8b-instruct-q5": "llama",
    "qwen-2.5-32b-instruct-q4": "qwen",
    "qwen-2.5-0.5b-instruct-q4": "qwen",
    "qwen-2.5-1.5b-instruct-q4": "qwen",
    "qwen-2.5-7b-instruct-q4": "qwen",
    "qwen-2.5-coder-7b-instruct-q4": "qwen",
    "llama-3.2-3b-instruct-q4": "llama",
    "gemma-2-2b-it-q4": "gemma",
    "phi-3.5-mini-instruct-q4": "phi",
}

# family+tier -> lineup slugs that supersede it (curated by hand alongside
# LINEUP; kept explicit rather than "closest size" heuristics so a maintainer
# can see exactly why a slug replaces another).
SUPERSEDES: dict[str, list[str]] = {
    "qwen3-1.7b-q4": ["qwen-2.5-0.5b-instruct-q4", "qwen-2.5-1.5b-instruct-q4"],
    "qwen3-4b-instruct-2507-q4": ["qwen-2.5-3b-instruct-q4"],
    "qwen3-8b-q4": ["qwen-2.5-7b-instruct-q4"],
    "qwen3-32b-q4": ["qwen-2.5-32b-instruct-q4"],
    "gemma-3-4b-it-q4": ["gemma-2-2b-it-q4"],
    "phi-4-mini-instruct-q4": ["phi-3.5-mini-instruct-q4"],
    # llama-3.2-3b-instruct-q4 and llama-3.1-8b-instruct-q5 are unchanged
    # (identical slug in both catalogs) -> no supersedes entry.
    # deepseek-r1-distill-llama-8b-q4, gemma-3-12b-it-q4, phi-4-q4,
    # ministral-8b-instruct-2410-q4, deepseek-r1-distill-qwen-32b-q4,
    # gemma-3-27b-it-q4 are all NEW additions -> no supersedes entry.
}


# ---------------------------------------------------------------------------
# Live resolution
# ---------------------------------------------------------------------------


@dataclass
class ResolvedModel:
    entry: LineupEntry
    size_bytes: int | None = None
    sha256: str | None = None
    released: str | None = None  # ISO date
    context_window: int | None = None
    num_layers: int | None = None
    num_layers_source: str = "unresolved"
    errors: list[str] = field(default_factory=list)


def _resolve_file_metadata(api: HfApi, entry: LineupEntry, resolved: ResolvedModel) -> None:
    try:
        info = api.model_info(entry.repo, files_metadata=True)
    except HfHubHTTPError as exc:
        resolved.errors.append(f"model_info({entry.repo}) failed: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - never crash the whole run
        resolved.errors.append(f"model_info({entry.repo}) failed: {type(exc).__name__}: {exc}")
        return

    resolved.released = info.lastModified.date().isoformat() if info.lastModified else None

    match = next((s for s in info.siblings if s.rfilename == entry.filename), None)
    if match is None:
        resolved.errors.append(
            f"filename '{entry.filename}' not found in {entry.repo} "
            f"(repo may have renamed/re-quantized its files)"
        )
        return

    resolved.size_bytes = match.size
    if match.lfs is not None and getattr(match.lfs, "sha256", None):
        resolved.sha256 = match.lfs.sha256
    else:
        resolved.errors.append(
            f"{entry.filename} has no LFS sha256 in Hub metadata; refusing to fabricate a hash"
        )


def _resolve_context_window(entry: LineupEntry, resolved: ResolvedModel) -> None:
    try:
        r = httpx.get(HF_GGUF_EXPAND_URL.format(repo=entry.repo), timeout=20)
        r.raise_for_status()
        gguf = r.json().get("gguf") or {}
        ctx = gguf.get("context_length")
        if isinstance(ctx, int) and ctx > 0:
            resolved.context_window = ctx
            return
    except Exception as exc:  # noqa: BLE001
        resolved.errors.append(f"gguf-header fetch failed for {entry.repo}: {type(exc).__name__}")

    resolved.context_window = entry.fallback_context_window
    resolved.errors.append(
        f"context_window unresolved from Hub; using documented fallback {entry.fallback_context_window}"
    )


def _resolve_num_layers(entry: LineupEntry, resolved: ResolvedModel) -> None:
    if entry.base_repo:
        try:
            path = hf_hub_download(entry.base_repo, "config.json")
            cfg = json.loads(Path(path).read_text())
            layers = cfg.get("num_hidden_layers") or (cfg.get("text_config") or {}).get(
                "num_hidden_layers"
            )
            if isinstance(layers, int) and layers > 0:
                resolved.num_layers = layers
                resolved.num_layers_source = "config.json"
                return
        except Exception as exc:  # noqa: BLE001 - gated repos 401 here; expected, not fatal
            resolved.errors.append(
                f"config.json unreachable for {entry.base_repo}: {type(exc).__name__}"
            )

    documented = DOCUMENTED_LAYERS.get(entry.base_repo or "")
    if documented is not None:
        resolved.num_layers = documented
        resolved.num_layers_source = "documented"
        return

    resolved.num_layers = entry.fallback_num_layers
    resolved.num_layers_source = "fallback"
    resolved.errors.append(
        f"num_layers unresolved; using fallback default {entry.fallback_num_layers}"
    )


def resolve_lineup(api: HfApi) -> list[ResolvedModel]:
    results = []
    for entry in LINEUP:
        resolved = ResolvedModel(entry=entry)
        _resolve_file_metadata(api, entry, resolved)
        _resolve_context_window(entry, resolved)
        _resolve_num_layers(entry, resolved)
        results.append(resolved)
    return results


def scan_drift(api: HfApi) -> dict[str, list[str]]:
    """For each family with a DRIFT_QUERIES entry, search HF for hits whose
    repo id actually contains the generation's match token (filters fuzzy
    HF-search noise -- see DRIFT_QUERIES docstring), AND whose owner is a
    trusted uploader (see TRUSTED_UPLOADERS -- drops community finetunes that
    token-match a next-gen name for a family with no real successor). Returns
    family -> list of matching repo ids (empty list = no drift detected).
    Network errors are swallowed per-family; a failed lookup just reports no
    drift found rather than crashing the whole scan."""
    hits: dict[str, list[str]] = {}
    for family, (query, token) in DRIFT_QUERIES.items():
        try:
            models = list(api.list_models(search=query, sort="downloads", direction=-1, limit=20))
            hits[family] = [
                m.id
                for m in models
                if token in _normalize(m.id)
                and m.id.split("/", 1)[0].lower() in TRUSTED_UPLOADERS
            ][:5]
        except Exception:  # noqa: BLE001
            hits[family] = []
    return hits


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------


def load_old_catalog() -> list[dict]:
    with CATALOG_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("models", [])


def build_report(resolved_lineup: list[ResolvedModel], old_catalog: list[dict], drift: dict[str, list[str]]) -> dict:
    old_by_slug = {m["slug"]: m for m in old_catalog}
    covered_old_slugs: set[str] = set()

    entries_report = []
    for r in resolved_lineup:
        slug = r.entry.slug
        old = old_by_slug.get(slug)
        superseded = SUPERSEDES.get(slug, [])
        covered_old_slugs.update(superseded)
        covered_old_slugs.add(slug) if old else None

        if old is not None:
            same_hash = r.sha256 is not None and old.get("sha256", "").lower() == r.sha256.lower()
            status = "UNCHANGED" if same_hash else "UPGRADE"
        elif superseded:
            status = "UPGRADE"
        else:
            status = "NEW"

        entries_report.append(
            {
                "slug": slug,
                "family": r.entry.family,
                "tier": r.entry.tier,
                "status": status,
                "supersedes": superseded,
                "repo": r.entry.repo,
                "filename": r.entry.filename,
                "size_bytes": r.size_bytes,
                "size_gb": round(r.size_bytes / 1e9, 2) if r.size_bytes else None,
                "sha256": r.sha256,
                "released": r.released,
                "context_window": r.context_window,
                "num_layers": r.num_layers,
                "num_layers_source": r.num_layers_source,
                "errors": r.errors,
            }
        )

    removed = [
        {"slug": m["slug"], "family": OLD_FAMILY.get(m["slug"], "unknown"), "tier": m.get("tier")}
        for m in old_catalog
        if m["slug"] not in covered_old_slugs
    ]

    drift_report = {family: hits for family, hits in drift.items() if hits}

    return {
        "generated_from": "scripts/check_model_updates.py",
        "old_catalog_count": len(old_catalog),
        "lineup_count": len(resolved_lineup),
        "entries": entries_report,
        "removed": removed,
        "drift": drift_report,
    }


def print_human_report(report: dict) -> None:
    by_tier: dict[str, list[dict]] = {"small": [], "medium": [], "large": []}
    for e in report["entries"]:
        by_tier.setdefault(e["tier"], []).append(e)

    print("=" * 78)
    print("MODEL CATALOG UPDATE CHECK")
    print(f"shipped catalog: {report['old_catalog_count']} models  |  lineup: {report['lineup_count']} models")
    print("=" * 78)

    for tier in ("small", "medium", "large"):
        entries = by_tier.get(tier, [])
        if not entries:
            continue
        print(f"\n-- {tier.upper()} --")
        for e in entries:
            marker = {"UPGRADE": "[UPGRADE]", "NEW": "[NEW]    ", "UNCHANGED": "[SAME]   "}[e["status"]]
            size = f"{e['size_gb']}GB" if e["size_gb"] is not None else "size?"
            print(f"  {marker} {e['slug']:38s} {size:>9s}  ({e['family']})")
            if e["supersedes"]:
                print(f"             supersedes: {', '.join(e['supersedes'])}")
            if e["errors"]:
                for err in e["errors"]:
                    print(f"             ! {err}")

    if report["removed"]:
        print("\n-- REMOVED (in shipped catalog, no lineup replacement) --")
        for r in report["removed"]:
            print(f"  [REMOVED]  {r['slug']:38s} ({r['family']}, {r['tier']})")

    print("\n-- DRIFT (HF has newer generations than LINEUP covers) --")
    if not report["drift"]:
        print("  none detected")
    else:
        for family, hits in report["drift"].items():
            print(f"  {family}: lineup may be stale. top hits for next-gen search:")
            for h in hits:
                print(f"      {h}")

    print()


# ---------------------------------------------------------------------------
# Catalog write
# ---------------------------------------------------------------------------


def build_proposed_catalog(resolved_lineup: list[ResolvedModel]) -> dict:
    models = []
    for r in resolved_lineup:
        e = r.entry
        if r.size_bytes is None or r.sha256 is None:
            # Refuse to emit an entry we can't verify; the report already
            # surfaces the error, and an unverified sha256 would defeat the
            # downloader's integrity gate.
            continue
        models.append(
            {
                "slug": e.slug,
                "name": e.name,
                "version": "1.0.0",
                "description": e.description,
                "tier": e.tier,
                "download_url": f"https://huggingface.co/{e.repo}/resolve/main/{e.filename}",
                "size_bytes": r.size_bytes,
                "sha256": r.sha256,
                "min_ram_gb": e.min_ram_gb,
                "min_vram_gb": e.min_vram_gb,
                "context_window": r.context_window,
                "num_layers": r.num_layers,
                "is_featured": e.is_featured,
                "recommended_tier": e.recommended_tier,
                "family": e.family,
                "released": r.released,
                "supersedes": SUPERSEDES.get(e.slug, []),
            }
        )
    return {"schema_version": SCHEMA_VERSION, "models": models}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON report")
    parser.add_argument(
        "--write",
        action="store_true",
        help="also write apps/desktop/python/potluck_local/data/models.json.proposed",
    )
    args = parser.parse_args()

    api = HfApi()
    old_catalog = load_old_catalog()
    resolved_lineup = resolve_lineup(api)
    drift = scan_drift(api)
    report = build_report(resolved_lineup, old_catalog, drift)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_human_report(report)

    if args.write:
        proposed = build_proposed_catalog(resolved_lineup)
        skipped = len(resolved_lineup) - len(proposed["models"])
        with PROPOSED_PATH.open("w", encoding="utf-8") as fh:
            json.dump(proposed, fh, indent=2)
            fh.write("\n")
        msg = f"Wrote {len(proposed['models'])} verified entries to {PROPOSED_PATH}"
        if skipped:
            msg += f"  ({skipped} skipped -- see errors above)"
        print(msg, file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
