"""Eval B — labeled corpus for retrieval quality measurement.

Corpus design:
  - ~50 memories spanning all MemoryCategory values
  - ~22 labeled queries (query text + gold memory id set)
  - Three query types:
      LEXICAL   — keyword alone finds them (control group)
      SEMANTIC  — zero token overlap; semantic is essential (the pnpm case)
      DISTRACTOR — topic-adjacent but wrong-answer memory present (precision test)
  - Two NEGATIVE controls: cross-project queries that must NOT return
    project-B memories when scoped to project-A.
  - Tier-1 memories (preference + instruction, high confidence) are mixed in
    so we can confirm they always surface in the always-retrieve slot.

Memory fields required for insert (see memory_db._CREATE_MEMORIES):
  id, content, category, confidence, source_type, source_conversation_id,
  created_at, last_accessed_at, access_count, is_active, project

Gold sets: list of memory ids that SHOULD appear in the result set.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------
# Each dict maps directly to a memories table row (subset of columns used at
# insert time).  `project` is None for global/cross-project memories, or a
# string like "backend" / "mobile" for scoped ones.

CORPUS: list[dict] = [
    # ---- Tier-1 standing prefs / instructions (always retrieve arm) --------
    {
        "id": "mem_t1_pnpm",
        "content": "Always use pnpm, never npm — npm corrupts the lockfile",
        "category": "instruction",
        "confidence": 1.0,
        "project": None,
    },
    {
        "id": "mem_t1_no_legacy_payments",
        "content": "Do not touch the legacy payments module — it is being deprecated next quarter",
        "category": "instruction",
        "confidence": 0.98,
        "project": None,
    },
    {
        "id": "mem_t1_dark_mode",
        "content": "Prefers dark mode in all UIs including editor and terminal",
        "category": "preference",
        "confidence": 0.95,
        "project": None,
    },
    {
        "id": "mem_t1_verbose_logs",
        "content": "Always enable verbose logging in dev; disable in production builds",
        "category": "instruction",
        "confidence": 0.92,
        "project": None,
    },
    # ---- Coding workflow facts / decisions (Tier-2, project-scoped) --------
    {
        "id": "mem_pg_decision",
        "content": "Decided to use Postgres over SQLite for the tasks table due to concurrency requirements",
        "category": "decision",
        "confidence": 0.95,
        "project": "backend",
    },
    {
        "id": "mem_auth_bearer",
        "content": "The auth API requires a Bearer token in the Authorization header for every request",
        "category": "fact",
        "confidence": 0.97,
        "project": "backend",
    },
    {
        "id": "mem_test_cmd",
        "content": "The test command for the backend is: make test-unit",
        "category": "instruction",
        "confidence": 0.99,
        "project": "backend",
    },
    {
        "id": "mem_deploy_fly",
        "content": "Deploy production via: flyctl deploy --remote-only",
        "category": "instruction",
        "confidence": 0.97,
        "project": "backend",
    },
    {
        "id": "mem_python_version",
        "content": "Backend requires Python 3.11; do not use 3.12 — dependency not yet compatible",
        "category": "instruction",
        "confidence": 0.93,
        "project": "backend",
    },
    {
        "id": "mem_ruff_linter",
        "content": "Use ruff for linting and formatting Python code; black is not used in this repo",
        "category": "preference",
        "confidence": 0.95,
        "project": "backend",
    },
    {
        "id": "mem_env_file",
        "content": "Copy contributor.env.example to .env before starting the sidecar; never commit .env",
        "category": "instruction",
        "confidence": 0.96,
        "project": "backend",
    },
    {
        "id": "mem_db_migrations",
        "content": "Database migrations run automatically on sidecar startup via init_memory_db; no manual step needed",
        "category": "fact",
        "confidence": 0.88,
        "project": "backend",
    },
    {
        "id": "mem_fastapi_port",
        "content": "The FastAPI sidecar listens on 127.0.0.1:8321",
        "category": "fact",
        "confidence": 0.99,
        "project": "backend",
    },
    {
        "id": "mem_frontend_vite",
        "content": "Frontend dev server runs on port 5173 via Vite",
        "category": "fact",
        "confidence": 0.96,
        "project": "backend",
    },
    {
        "id": "mem_wal_mode",
        "content": "SQLite is configured with WAL journal mode to allow concurrent reads and writes",
        "category": "fact",
        "confidence": 0.87,
        "project": "backend",
    },
    # ---- Semantic-gap memories (zero keyword overlap with their queries) -----
    # These are the critical memories that keyword-only will MISS.
    {
        "id": "mem_sem_react_not_vue",
        "content": "The team chose React for all new UI work; no new Vue components should be written",
        "category": "decision",
        "confidence": 0.90,
        "project": "backend",
    },
    {
        "id": "mem_sem_ts_strict",
        "content": "TypeScript strict mode is mandatory in this repository",
        "category": "instruction",
        "confidence": 0.94,
        "project": "backend",
    },
    {
        "id": "mem_sem_no_console_log",
        "content": "Remove all console.log statements before committing; use the logger utility instead",
        "category": "instruction",
        "confidence": 0.91,
        "project": "backend",
    },
    {
        "id": "mem_sem_api_versioning",
        "content": "All REST endpoints must be prefixed with /v1/ — no unversioned routes allowed",
        "category": "instruction",
        "confidence": 0.93,
        "project": "backend",
    },
    {
        "id": "mem_sem_env_never_hardcode",
        "content": "Credentials and API keys must come from environment variables — never hardcode secrets in source",
        "category": "instruction",
        "confidence": 0.99,
        "project": "backend",
    },
    {
        "id": "mem_sem_gpu_apple",
        "content": "On Apple Silicon, always set n_gpu_layers=-1 to use the Metal backend for inference",
        "category": "instruction",
        "confidence": 0.96,
        "project": "backend",
    },
    {
        "id": "mem_sem_branch_main",
        "content": "The primary branch is main; never push feature branches directly — always open a pull request",
        "category": "instruction",
        "confidence": 0.92,
        "project": "backend",
    },
    {
        "id": "mem_sem_pr_review",
        "content": "All code changes require at least one review approval before merge, no self-merges",
        "category": "instruction",
        "confidence": 0.90,
        "project": "backend",
    },
    # ---- Distractor memories (same topic, wrong answer — tests precision) ---
    # These exist alongside a correct memory so the retriever must rank the
    # right one higher.
    {
        "id": "mem_dist_sqlite_fine",
        "content": "SQLite is acceptable for read-heavy reporting tables with low write concurrency",
        "category": "decision",
        "confidence": 0.80,
        "project": "backend",
    },  # distractor for mem_pg_decision (postgres vs sqlite)
    {
        "id": "mem_dist_npm_ok_scripts",
        "content": "npm is fine for running one-off scripts outside the monorepo context",
        "category": "preference",
        "confidence": 0.70,
        "project": None,
    },  # distractor for mem_t1_pnpm — recall pnpm but don't return this as gold
    {
        "id": "mem_dist_python312_ok",
        "content": "Python 3.12 is used in the coordinator service",
        "category": "fact",
        "confidence": 0.85,
        "project": "backend",
    },  # distractor for mem_python_version
    # ---- Project context memories -------------------------------------------
    {
        "id": "mem_proj_repo_name",
        "content": "The GitHub repository is named potluckai and hosted at github.com/potluck-ai/potluckai",
        "category": "project",
        "confidence": 0.90,
        "project": "backend",
    },
    {
        "id": "mem_proj_team_size",
        "content": "The engineering team has three full-time engineers plus two part-time contributors",
        "category": "project",
        "confidence": 0.82,
        "project": "backend",
    },
    {
        "id": "mem_proj_launch_q3",
        "content": "Public launch is targeted for Q3; no public users before that date",
        "category": "project",
        "confidence": 0.88,
        "project": "backend",
    },
    # ---- Person memories ----------------------------------------------------
    {
        "id": "mem_person_rob",
        "content": "Rob is the primary maintainer of the Python sidecar and prefers concise code reviews",
        "category": "person",
        "confidence": 0.85,
        "project": "backend",
    },
    # ---- Cross-project isolation — project B (mobile) -----------------------
    # These must NOT appear in queries scoped to project "backend".
    {
        "id": "mem_mobile_kotlin",
        "content": "Mobile app uses Kotlin coroutines for all async work — no RxJava",
        "category": "instruction",
        "confidence": 0.93,
        "project": "mobile",
    },
    {
        "id": "mem_mobile_deploy",
        "content": "Deploy mobile releases via Fastlane; the lane is called release_beta",
        "category": "instruction",
        "confidence": 0.91,
        "project": "mobile",
    },
    {
        "id": "mem_mobile_pnpm",
        "content": "Mobile monorepo also uses pnpm for the React Native JS layer",
        "category": "instruction",
        "confidence": 0.88,
        "project": "mobile",
    },
    # ---- Additional corpus padding ------------------------------------------
    {
        "id": "mem_docker_compose",
        "content": "Use docker compose (v2 plugin) not docker-compose (v1 binary) — the v1 binary is deprecated",
        "category": "instruction",
        "confidence": 0.89,
        "project": "backend",
    },
    {
        "id": "mem_gguf_q4km",
        "content": "Default model quantization for local inference is Q4_K_M — best quality-to-size ratio for 8GB RAM",
        "category": "preference",
        "confidence": 0.87,
        "project": "backend",
    },
    {
        "id": "mem_context_window",
        "content": "Set n_ctx=4096 on 8GB systems; can push to 8192 on 16GB RAM or Apple Silicon 16GB",
        "category": "fact",
        "confidence": 0.85,
        "project": "backend",
    },
    {
        "id": "mem_ci_github_actions",
        "content": "CI runs on GitHub Actions; the workflow files are in .github/workflows/",
        "category": "fact",
        "confidence": 0.86,
        "project": "backend",
    },
    {
        "id": "mem_makefile_dev",
        "content": "Run make dev to start the full stack: Postgres, cloud API, sidecar, Electron, and Vite",
        "category": "instruction",
        "confidence": 0.95,
        "project": "backend",
    },
    {
        "id": "mem_sentry_dsn",
        "content": "Error tracking uses Sentry; DSN is set via SENTRY_DSN environment variable",
        "category": "fact",
        "confidence": 0.83,
        "project": "backend",
    },
    {
        "id": "mem_openapi_docs",
        "content": "FastAPI auto-generates OpenAPI docs at /docs and /redoc endpoints in development mode",
        "category": "fact",
        "confidence": 0.81,
        "project": "backend",
    },
    {
        "id": "mem_no_async_in_sync",
        "content": "Never call asyncio.run() inside an already-running event loop — use run_in_executor instead",
        "category": "instruction",
        "confidence": 0.90,
        "project": "backend",
    },
    {
        "id": "mem_type_annotations",
        "content": "All Python functions must have full type annotations; mypy strict mode is enforced",
        "category": "instruction",
        "confidence": 0.91,
        "project": "backend",
    },
]


# ---------------------------------------------------------------------------
# Labeled queries
# ---------------------------------------------------------------------------
# Each query has:
#   id         — stable identifier for per-query reporting
#   text       — the query string (simulates what an agent or user would ask)
#   gold       — set of memory ids that SHOULD be retrieved
#   type       — LEXICAL | SEMANTIC | DISTRACTOR | TIER1 | NEGATIVE
#   project    — project scope for the retrieval call (None = global only)
#   note       — explains why this query is in the set

QUERIES: list[dict] = [
    # -----------------------------------------------------------------------
    # LEXICAL — keyword alone should find these (control group)
    # -----------------------------------------------------------------------
    {
        "id": "q_lexical_pnpm",
        "text": "pnpm install",
        "gold": {"mem_t1_pnpm"},
        "type": "LEXICAL",
        "project": "backend",
        "note": "Direct keyword match on 'pnpm'",
    },
    {
        "id": "q_lexical_auth",
        "text": "auth Bearer token authorization header",
        "gold": {"mem_auth_bearer"},
        "type": "LEXICAL",
        "project": "backend",
        "note": "All key tokens present in memory content",
    },
    {
        "id": "q_lexical_deploy",
        "text": "flyctl deploy command",
        "gold": {"mem_deploy_fly"},
        "type": "LEXICAL",
        "project": "backend",
        "note": "flyctl and deploy both present in content",
    },
    {
        "id": "q_lexical_test",
        "text": "make test-unit run tests",
        "gold": {"mem_test_cmd"},
        "type": "LEXICAL",
        "project": "backend",
        "note": "make test-unit verbatim in content",
    },
    {
        "id": "q_lexical_port",
        "text": "sidecar port 8321",
        "gold": {"mem_fastapi_port"},
        "type": "LEXICAL",
        "project": "backend",
        "note": "port number + sidecar token both in content",
    },
    {
        "id": "q_lexical_env",
        "text": "contributor.env.example .env sidecar",
        "gold": {"mem_env_file"},
        "type": "LEXICAL",
        "project": "backend",
        "note": "exact filename token overlap",
    },
    {
        "id": "q_lexical_makefile",
        "text": "make dev start full stack",
        "gold": {"mem_makefile_dev"},
        "type": "LEXICAL",
        "project": "backend",
        "note": "make dev verbatim in content",
    },

    # -----------------------------------------------------------------------
    # SEMANTIC — zero keyword overlap; semantic arm is essential
    # -----------------------------------------------------------------------
    {
        "id": "q_sem_package_manager",
        "text": "what package manager should I use",
        "gold": {"mem_t1_pnpm"},
        "type": "SEMANTIC",
        "project": "backend",
        "note": "No token overlap with 'pnpm/npm/lockfile'; only semantics link 'package manager' -> pnpm instruction",
    },
    {
        "id": "q_sem_database_choice",
        "text": "which database should we use for new tables",
        "gold": {"mem_pg_decision"},
        "type": "SEMANTIC",
        "project": "backend",
        "note": "No 'postgres/sqlite/tasks/concurrency' tokens in query; semantic link via database selection intent",
    },
    {
        "id": "q_sem_frontend_framework",
        "text": "can I write this component in Vue",
        "gold": {"mem_sem_react_not_vue"},
        "type": "SEMANTIC",
        "project": "backend",
        "note": "Vue is in memory content as the rejected choice; query asks about writing Vue — semantic should catch it",
    },
    {
        "id": "q_sem_type_safety",
        "text": "do I need to add TypeScript types to this file",
        "gold": {"mem_sem_ts_strict", "mem_type_annotations"},
        "type": "SEMANTIC",
        "project": "backend",
        "note": "'TypeScript' appears in mem_sem_ts_strict but not in query; 'type annotations' is semantic link to mem_type_annotations",
    },
    {
        "id": "q_sem_debug_print",
        "text": "is it ok to leave print statements in my code",
        "gold": {"mem_sem_no_console_log"},
        "type": "SEMANTIC",
        "project": "backend",
        "note": "No overlap: 'print' vs 'console.log' — semantic equivalence across languages",
    },
    {
        "id": "q_sem_api_path",
        "text": "how should I name my new API endpoint",
        "gold": {"mem_sem_api_versioning"},
        "type": "SEMANTIC",
        "project": "backend",
        "note": "No token overlap with '/v1/ prefix' rule; semantic link via API design intent",
    },
    {
        "id": "q_sem_credentials",
        "text": "where should I put the API key in the code",
        "gold": {"mem_sem_env_never_hardcode"},
        "type": "SEMANTIC",
        "project": "backend",
        "note": "'API key' + 'in the code' semantically links to 'never hardcode secrets' instruction",
    },
    {
        "id": "q_sem_gpu_inference",
        "text": "how do I speed up model inference on my Mac",
        "gold": {"mem_sem_gpu_apple"},
        "type": "SEMANTIC",
        "project": "backend",
        "note": "No token overlap: 'Mac' -> 'Apple Silicon', 'speed up inference' -> 'Metal backend n_gpu_layers=-1'",
    },
    {
        "id": "q_sem_merge_workflow",
        "text": "can I push directly to main",
        "gold": {"mem_sem_branch_main", "mem_sem_pr_review"},
        "type": "SEMANTIC",
        "project": "backend",
        "note": "'push to main' semantically maps to PR-required and no-self-merge rules; 'main' appears in content but 'push directly' does not",
    },
    {
        "id": "q_sem_touch_payments",
        "text": "I need to modify the billing code",
        "gold": {"mem_t1_no_legacy_payments"},
        "type": "SEMANTIC",
        "project": "backend",
        "note": "'billing code' -> 'legacy payments module'; no token overlap",
    },

    # -----------------------------------------------------------------------
    # DISTRACTOR — correct memory + wrong-answer memory on same topic
    # -----------------------------------------------------------------------
    {
        "id": "q_dist_database",
        "text": "postgres sqlite database decision",
        "gold": {"mem_pg_decision"},
        "type": "DISTRACTOR",
        "project": "backend",
        "note": "mem_dist_sqlite_fine is also topically related; gold is the decision, distractor is the exception",
    },
    {
        "id": "q_dist_python",
        "text": "which Python version to use",
        "gold": {"mem_python_version"},
        "type": "DISTRACTOR",
        "project": "backend",
        "note": "mem_dist_python312_ok is in corpus and mentions 3.12; should not surface as the primary answer",
    },
    {
        "id": "q_dist_pnpm_scripts",
        "text": "should I use npm or pnpm",
        "gold": {"mem_t1_pnpm"},
        "type": "DISTRACTOR",
        "project": "backend",
        "note": "mem_dist_npm_ok_scripts is the distractor (npm is fine for one-off scripts); gold is the global instruction",
    },

    # -----------------------------------------------------------------------
    # TIER1 — standing preferences + instructions; must always surface
    # -----------------------------------------------------------------------
    {
        "id": "q_tier1_prefs",
        "text": "general coding preferences for this project",
        "gold": {"mem_t1_pnpm", "mem_t1_no_legacy_payments", "mem_t1_dark_mode", "mem_t1_verbose_logs"},
        "type": "TIER1",
        "project": "backend",
        "note": "All four Tier-1 memories should always appear in the always-retrieve slot",
    },

    # -----------------------------------------------------------------------
    # NEGATIVE — cross-project isolation; MUST NOT return project-mobile rows
    # -----------------------------------------------------------------------
    {
        "id": "q_neg_mobile_leak",
        "text": "deploy command for release",
        "gold": {"mem_deploy_fly"},  # correct answer is backend deploy
        "type": "NEGATIVE",
        "project": "backend",
        "note": "mem_mobile_deploy mentions 'deploy' + 'release_beta' — must NOT appear when scoped to backend",
    },
    {
        "id": "q_neg_mobile_pnpm",
        "text": "pnpm usage in the repo",
        "gold": {"mem_t1_pnpm"},  # correct answer is global pnpm instruction
        "type": "NEGATIVE",
        "project": "backend",
        "note": "mem_mobile_pnpm is project=mobile and must not bleed into backend scope",
    },
    # -----------------------------------------------------------------------
    # AMBIENT — a standing coding-convention that SHOULD apply to the task,
    # but the query has zero lexical/semantic overlap with the rule. Pure
    # relevance retrieval CANNOT surface these — only the always-preload can.
    # This is the case the Tier-1 preload exists for. The gold rules here are
    # mid-confidence (0.91, 0.95) — NOT in the top-3 — so they surface at a
    # generous always_budget but get squeezed out at a tight one. These
    # queries quantify the cost of lowering always_budget: query-relevant
    # recall rises but ambient-context injection falls.
    # -----------------------------------------------------------------------
    {
        "id": "q_amb_typeann",
        "text": "implement pagination for the orders list endpoint",
        "gold": {"mem_type_annotations"},
        "type": "AMBIENT",
        "project": "backend",
        "note": "Writing a Python endpoint -> the type-annotation rule applies, but the query shares no tokens/meaning with 'type annotations / mypy'. Only the preload can inject it.",
    },
    {
        "id": "q_amb_ruff",
        "text": "build a retry wrapper around the http client",
        "gold": {"mem_ruff_linter"},
        "type": "AMBIENT",
        "project": "backend",
        "note": "Writing Python -> the ruff/lint rule applies, but no overlap with 'ruff / linting / formatting'. Only the preload can inject it.",
    },
]
