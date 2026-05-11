# Potluck AI — one-command dev ergonomics.
#
# Backend (Postgres + cloud API) runs in Docker.
# Desktop stack (Vite + Python sidecar + Electron) runs natively on the host —
# it needs GPU access and a real window.

# Python interpreter used to create the sidecar venv.
# Override on the command line if your default python3 is too old:
#   PYTHON_BIN=python3.11 make setup
# Auto-picks python3.11 if on PATH, else falls back to python3.
PYTHON_BIN ?= $(shell command -v python3.11 2>/dev/null || command -v python3)

# Docker Compose invocation. Prefers the v2 plugin (`docker compose`);
# falls back to the legacy `docker-compose` v1 if that's all that's available.
# Override explicitly if neither autodetects (e.g. COMPOSE="colima docker compose").
COMPOSE ?= $(shell \
  if docker compose version >/dev/null 2>&1; then echo "docker compose"; \
  elif command -v docker-compose >/dev/null 2>&1; then echo "docker-compose"; \
  fi)

.PHONY: help setup check-python check-docker up down restart logs api-logs pg-logs coord-logs migrate psql \
        api-shell coord-shell test-api test-coordinator m3-smoke lint install-hooks dev desktop frontend \
        sidecar sidecar-setup sidecar-go coordinator coordinator-setup frontend-setup electron-setup ps status clean reset

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------- one-time setup ----------

setup: check-python sidecar-setup frontend-setup electron-setup ## One-time: create Python venv + install node deps for the desktop stack
	@echo "==> Setup complete."
	@echo "   Backend:  make up"
	@echo "   Desktop:  make desktop   (or 'make dev' for both in one terminal)"

check-python: ## Verify PYTHON_BIN points to Python 3.11+
	@if [ -z "$(PYTHON_BIN)" ]; then \
		echo "error: no python interpreter found. install python 3.11+ (e.g. 'brew install python@3.11') or pass PYTHON_BIN=/path/to/python"; \
		exit 1; \
	fi
	@PYVER=$$($(PYTHON_BIN) -c 'import sys; print("%d.%d" % sys.version_info[:2])'); \
	MAJOR=$${PYVER%%.*}; MINOR=$${PYVER##*.}; \
	if [ "$$MAJOR" -lt 3 ] || { [ "$$MAJOR" -eq 3 ] && [ "$$MINOR" -lt 11 ]; }; then \
		echo "error: $(PYTHON_BIN) is Python $$PYVER; need 3.11+. try: PYTHON_BIN=python3.11 make setup"; \
		exit 1; \
	fi
	@echo "==> Using $(PYTHON_BIN) ($$($(PYTHON_BIN) --version))"

sidecar-setup: check-python ## Create Python venv for the sidecar and install requirements
	@echo "==> Creating Python sidecar venv (apps/desktop/python) with $(PYTHON_BIN)"
	@cd apps/desktop/python && $(PYTHON_BIN) -m venv .venv
	@cd apps/desktop/python && ./.venv/bin/pip install --upgrade pip
	@cd apps/desktop/python && ./.venv/bin/pip install -r requirements.txt

frontend-setup: ## Install frontend node deps
	@echo "==> Installing frontend deps (apps/desktop/frontend)"
	@cd apps/desktop/frontend && pnpm install

electron-setup: ## Install electron node deps
	@echo "==> Installing electron deps (apps/desktop/electron)"
	@cd apps/desktop/electron && pnpm install

# ---------- backend (Postgres + API in Docker) ----------

check-docker: ## Verify docker + a compose command are available and the daemon is running
	@if ! command -v docker >/dev/null 2>&1; then \
		echo "error: docker CLI not found. install Docker Desktop: brew install --cask docker"; \
		exit 1; \
	fi
	@if [ -z "$(COMPOSE)" ]; then \
		echo "error: neither 'docker compose' (v2 plugin) nor 'docker-compose' (v1) is available."; \
		echo "       install Docker Desktop (includes the compose plugin): brew install --cask docker"; \
		exit 1; \
	fi
	@if ! docker info >/dev/null 2>&1; then \
		echo "error: docker daemon is not running. open Docker Desktop: open -a Docker"; \
		exit 1; \
	fi
	@echo "==> Using '$(COMPOSE)'"

up: check-docker ## Start Postgres + API + Coordinator in the background (runs migrations first)
	$(COMPOSE) up -d --build
	@echo "==> API:         http://localhost:8080/health"
	@echo "==> Coordinator: http://localhost:8400/health (LAN-reachable on this host's IP)"
	@echo "==> Postgres:    localhost:5432 (potluck/potluck)"

down: check-docker ## Stop the backend stack (preserves data volume)
	$(COMPOSE) down

restart: check-docker ## Rebuild + restart the API container
	$(COMPOSE) up -d --build api

logs: check-docker ## Tail all backend logs
	$(COMPOSE) logs -f

api-logs: check-docker ## Tail only the API logs
	$(COMPOSE) logs -f api

coord-logs: check-docker ## Tail only the Coordinator logs
	$(COMPOSE) logs -f coordinator

pg-logs: check-docker ## Tail only Postgres logs
	$(COMPOSE) logs -f postgres

migrate: check-docker ## Re-run Alembic migrations inside a one-shot container
	$(COMPOSE) run --rm migrate

psql: check-docker ## Open a psql shell against the dev database
	$(COMPOSE) exec postgres psql -U potluck -d potluck

api-shell: check-docker ## Open a bash shell inside the api container
	$(COMPOSE) exec api bash

coord-shell: check-docker ## Open a bash shell inside the coordinator container
	$(COMPOSE) exec coordinator bash

test-api: check-docker ## Run pytest inside the api container
	$(COMPOSE) exec api pytest -q

test-coordinator: ## Run coordinator pytest suite (privacy invariants etc.) in its venv
	@if [ ! -x services/coordinator/.venv/bin/pytest ]; then \
		echo "==> services/coordinator/.venv/bin/pytest missing — run 'make coordinator-setup' first"; \
		exit 1; \
	fi
	@cd services/coordinator && ./.venv/bin/pytest -q

m3-smoke: ## End-to-end Phase G m3 acceptance check (needs coordinator + at least one peer running)
	@python3 scripts/m3-smoke.py $(M3_ARGS)

mesh-smoke: ## End-to-end Phase G mesh week-1 acceptance (needs coordinator + Postgres)
	@python3 scripts/mesh-smoke.py $(MESH_ARGS)

lint: ## Run the privacy-invariant lint (member_id leaks + contributor in-memory-only). Free, local.
	@python3 scripts/lint_privacy.py

install-hooks: ## Install git hooks (pre-commit privacy lint) into .git/hooks/
	@if [ ! -d .git ]; then \
		echo "error: not in a git repo. cd to the repo root and try again."; \
		exit 1; \
	fi
	@cp scripts/git-hooks/pre-commit .git/hooks/pre-commit
	@chmod +x .git/hooks/pre-commit
	@echo "==> installed .git/hooks/pre-commit"
	@echo "    runs 'make lint' on every commit. bypass once with 'git commit --no-verify'."

ps: status ## Alias for status
status: check-docker ## Show backend container status
	$(COMPOSE) ps

# ---------- desktop (native) ----------

desktop: ## Start the full desktop stack (Vite + Python sidecar + Electron) in this terminal
	cd apps/desktop/electron && pnpm dev

frontend: ## Start only the Vite dev server (useful when iterating on UI without Electron)
	cd apps/desktop/frontend && pnpm dev

sidecar: ## Start only the Python sidecar (useful when debugging inference)
	cd apps/desktop/python && ./.venv/bin/python -m potluck_local.app --port 8321

sidecar-go: ## Build the Go mesh sidecar for the current platform (output: apps/desktop/go/potluck-mesh/dist/)
	@if [ ! -f apps/desktop/go/potluck-mesh/go.sum ]; then \
		echo "==> go.sum missing — running go mod tidy first"; \
		cd apps/desktop/go/potluck-mesh && go mod tidy; \
	fi
	@echo "==> Building potluck-mesh for $$(go env GOOS)/$$(go env GOARCH)"
	$(MAKE) -C apps/desktop/go/potluck-mesh build

# ---------- coordinator (Phase G control plane, native run) ----------

coordinator-setup: check-python ## Create Python venv for the coordinator and install dev requirements
	@echo "==> Creating Python coordinator venv (services/coordinator) with $(PYTHON_BIN)"
	@cd services/coordinator && $(PYTHON_BIN) -m venv .venv
	@cd services/coordinator && ./.venv/bin/pip install --upgrade pip
	@cd services/coordinator && ./.venv/bin/pip install -r requirements-dev.txt

coordinator: ## Start the coordinator natively against docker-composed Postgres (binds 0.0.0.0:8400)
	cd services/coordinator && \
	  DATABASE_URL=postgresql+asyncpg://potluck:potluck@localhost:5432/potluck \
	  ENV=development \
	  ./.venv/bin/uvicorn coordinator.app:create_app --factory --host 0.0.0.0 --port 8400 --reload

# ---------- full-stack dev (backend + desktop in one terminal) ----------

dev: ## Start backend in docker + desktop stack natively (one terminal, Ctrl-C stops desktop, backend keeps running)
	@$(MAKE) up
	@echo
	@echo "==> Backend is up in the background. Starting desktop stack (Ctrl-C to stop)…"
	@echo "   Run 'make down' when you are done to stop the backend."
	@echo
	@$(MAKE) desktop

# ---------- cleanup ----------

clean: ## Remove build artifacts (keeps the db volume)
	-$(COMPOSE) down
	rm -rf apps/desktop/frontend/dist
	rm -rf apps/desktop/electron/out apps/desktop/electron/resources/python
	rm -rf apps/desktop/python/dist apps/desktop/python/build

reset: ## Full nuke: stop containers, drop db volume, wipe build artifacts (asks for confirmation)
	@read -p "This will delete the Postgres volume and all local build artifacts. Continue? [y/N] " ans && [ $${ans:-N} = y ]
	-$(COMPOSE) down -v
	rm -rf apps/desktop/frontend/dist apps/desktop/frontend/node_modules
	rm -rf apps/desktop/electron/out apps/desktop/electron/resources/python apps/desktop/electron/node_modules
	rm -rf apps/desktop/python/.venv apps/desktop/python/dist apps/desktop/python/build

.DEFAULT_GOAL := help
