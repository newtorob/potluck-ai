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
        api-shell coord-shell test-api test-coordinator m3-smoke mesh-smoke lint install-hooks dev desktop frontend \
        sidecar sidecar-setup sidecar-go installer-deps installer-bundle installer-mac installer-win installer-linux installer-clean \
        coordinator coordinator-setup frontend-setup electron-setup ps status clean reset eval-retrieval bench \
        bench-inference bench-inference-get-models \
        demo-setup demo-capture demo-video

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

eval-retrieval: ## Eval B — retrieval quality: RRF vs keyword-only on labeled corpus
	@if [ ! -x apps/desktop/python/.venv/bin/python ]; then \
		echo "==> sidecar venv not found — run 'make sidecar-setup' first"; \
		exit 1; \
	fi
	@cd apps/desktop/python && .venv/bin/python -m evals.eval_retrieval_quality

bench: ## LongMemEval retrieval benchmark — scores Potluck retriever on 500 oracle questions
	@if [ ! -x apps/desktop/python/.venv/bin/python ]; then \
		echo "==> sidecar venv not found — run 'make sidecar-setup' first"; \
		exit 1; \
	fi
	@if [ ! -f bench/longmemeval/data/longmemeval_oracle.json ]; then \
		echo "==> Downloading LongMemEval oracle dataset..."; \
		curl -L --max-time 120 \
		  "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json" \
		  -o bench/longmemeval/data/longmemeval_oracle.json; \
	fi
	@echo "==> Running LongMemEval oracle (500 instances, k=10)..."
	@PYTHONPATH=. apps/desktop/python/.venv/bin/python bench/run_longmemeval.py \
		--data bench/longmemeval/data/longmemeval_oracle.json \
		--k 10 \
		--verbose

bench-inference-get-models: ## Download GGUF models for inference bench (small models only; ~12 GB)
	@if [ ! -x apps/desktop/python/.venv/bin/python ]; then \
		echo "==> sidecar venv not found — run 'make sidecar-setup' first"; \
		exit 1; \
	fi
	@echo "==> Downloading inference benchmark models (Phi-3-mini, Llama-3.1-8B, Qwen2.5-7B)..."
	@PYTHONPATH=. apps/desktop/python/.venv/bin/python bench/scripts/get_models.py --model all-small

bench-inference: ## Local-model inference benchmark — gen/prompt-eval tok/s, TTFT, RSS across 3 models (Mac only)
	@if [ ! -x apps/desktop/python/.venv/bin/python ]; then \
		echo "==> sidecar venv not found — run 'make sidecar-setup' first"; \
		exit 1; \
	fi
	@if [ -z "$$(ls bench/models/*.gguf 2>/dev/null)" ]; then \
		echo "==> No GGUF files found in bench/models/ — run 'make bench-inference-get-models' first"; \
		exit 1; \
	fi
	@echo "==> Running inference benchmark..."
	@if [ -n "$(MODEL)" ]; then \
		PYTHONPATH=. PYTHONUNBUFFERED=1 apps/desktop/python/.venv/bin/python bench/run_inference.py \
			--model "$(MODEL)" \
			--prompts bench/prompts/inference_v1.json \
			--runs 3; \
	else \
		PYTHONPATH=. PYTHONUNBUFFERED=1 apps/desktop/python/.venv/bin/python bench/run_inference.py \
			--prompts bench/prompts/inference_v1.json \
			--runs 3; \
	fi

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

# ---------- installer pipeline (unsigned dev builds — signing requires certs) ----------

installer-deps: ## Install build-time tools (pyinstaller) from requirements-build.txt; verify go + pnpm present
	@command -v $(PYTHON_BIN) >/dev/null 2>&1 || { echo "error: $(PYTHON_BIN) not found"; exit 1; }
	@command -v go >/dev/null 2>&1 || { echo "error: go not installed. brew install go"; exit 1; }
	@command -v pnpm >/dev/null 2>&1 || { echo "error: pnpm not installed. brew install pnpm"; exit 1; }
	@echo "==> Installing build-time deps from requirements-build.txt"
	@cd apps/desktop/python && ./.venv/bin/pip install -r requirements-build.txt --quiet
	@echo "==> Installer build deps present."

installer-bundle: installer-deps sidecar-go ## Bundle both Python sidecar binaries + Go sidecar into apps/desktop/electron/resources/
	@echo "==> Building Python sidecar binary (potluck-local, PyInstaller --onedir)"
	@cd apps/desktop/python && ./.venv/bin/python build.py
	@echo "==> Building MCP sidecar binary (potluck-mcp, PyInstaller --onedir)"
	@cd apps/desktop/python && ./.venv/bin/python build-mcp.py
	@echo "==> Building frontend bundle"
	@cd apps/desktop/frontend && pnpm exec vite build
	@echo "==> Staging into apps/desktop/electron/resources/"
	@cd apps/desktop/electron && pnpm run prepack:python && pnpm run prepack:mcp && pnpm run prepack:mesh

installer-mac: installer-bundle ## Build unsigned macOS installer (.dmg + .zip). Signing requires Apple Developer cert.
	@echo "==> Building macOS installer (unsigned — needs CSC_LINK + APPLE_ID env for signing)"
	@cd apps/desktop/electron && CSC_IDENTITY_AUTO_DISCOVERY=false pnpm run build:mac

installer-win: installer-bundle ## Build unsigned Windows installer (.exe via NSIS). Signing requires EV cert.
	@echo "==> Building Windows installer (unsigned — needs CSC_LINK env for signing)"
	@cd apps/desktop/electron && CSC_IDENTITY_AUTO_DISCOVERY=false pnpm run build:win

installer-linux: installer-bundle ## Build Linux installers (.AppImage + .deb + .rpm)
	@echo "==> Building Linux installers (AppImage + deb + rpm)"
	@cd apps/desktop/electron && pnpm run build:linux

installer-clean: ## Remove staged installer artifacts + electron-builder output
	@cd apps/desktop/electron && pnpm run clean
	@rm -rf apps/desktop/python/dist apps/desktop/python/build
	@rm -rf apps/desktop/go/potluck-mesh/dist

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

# ---------- demo video pipeline ----------

demo-setup: ## Install Playwright + deps for the demo-video harness
	cd tools/demo-video && pnpm install && pnpm exec playwright install chromium

demo-capture: ## Capture Demo A silent .webm (requires sidecar + Vite dev server running)
	cd tools/demo-video && pnpm run capture

demo-video: ## Compose narrated mp4 (add audio/scene-NN.mp3 clips first; degrades to silent mp4 if absent)
	cd tools/demo-video && bash compose.sh

.DEFAULT_GOAL := help
