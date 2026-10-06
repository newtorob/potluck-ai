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
        api-shell coord-shell test test-fast test-api test-coordinator test-sidecar test-go test-frontend test-cli test-landing m3-smoke mesh-smoke lint smoke-prod install-hooks dev desktop frontend \
        sidecar sidecar-setup sidecar-go installer-deps installer-bundle installer-mac installer-mac-dir installer-win installer-linux installer-linux-docker installer-clean \
        install-mac-app kill dogfood \
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
	@cd apps/desktop/python && ./.venv/bin/python scripts/vendor_gpu_backend.py

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

# ---------- tailnet (cross-machine Phase G mesh dev) ----------

tailnet-ip: ## Print this host's Tailscale IPv4 (used by the tailnet targets)
	@ip="$$(tailscale ip -4 2>/dev/null | head -1)"; \
	if [ -z "$$ip" ]; then \
		echo "error: 'tailscale ip -4' returned nothing. Is Tailscale up? (tailscale status)"; exit 1; \
	fi; \
	echo "$$ip"

up-tailnet: check-docker ## Start the backend with coordinator+DERP published on the Tailscale IP (for peers like newtorob)
	@ip="$$(tailscale ip -4 2>/dev/null | head -1)"; \
	if [ -z "$$ip" ]; then \
		echo "error: 'tailscale ip -4' returned nothing. Is Tailscale up? (tailscale status)"; exit 1; \
	fi; \
	echo "==> Publishing coordinator(8400) + DERP(8443/8444) on tailnet IP $$ip + 127.0.0.1"; \
	POTLUCK_TAILNET_IP="$$ip" $(COMPOSE) -f docker-compose.yml -f docker-compose.tailnet.yml up -d --build
	@echo
	@$(MAKE) tailnet-check

tailnet-check: ## Verify coordinator(8400)+DERP(8443) are reachable on localhost AND the tailnet IP
	@ip="$$(tailscale ip -4 2>/dev/null | head -1)"; \
	if [ -z "$$ip" ]; then echo "error: no tailscale IP"; exit 1; fi; \
	echo "==> Reachability matrix (host = this Mac, peer = whatever can hit \$$ip):"; \
	printf "    %-34s " "8400 coordinator @ 127.0.0.1:"; \
	curl -s -o /dev/null -w "%{http_code}\n" --max-time 4 http://127.0.0.1:8400/health || echo "000"; \
	printf "    %-34s " "8400 coordinator @ $$ip:"; \
	curl -s -o /dev/null -w "%{http_code}\n" --max-time 4 http://$$ip:8400/health || echo "000"; \
	printf "    %-34s " "8443 DERP tcp @ 127.0.0.1:"; \
	(nc -z -w4 127.0.0.1 8443 >/dev/null 2>&1 && echo "OPEN") || echo "CLOSED"; \
	printf "    %-34s " "8443 DERP tcp @ $$ip:"; \
	(nc -z -w4 $$ip 8443 >/dev/null 2>&1 && echo "OPEN") || echo "CLOSED"; \
	echo; \
	echo "==> A tailnet PEER should also see 200/OPEN on $$ip. If the peer sees 000/CLOSED"; \
	echo "    but localhost is 200/OPEN, the break is almost certainly on the PEER (a VPN"; \
	echo "    like Mullvad hijacking the 100.64.0.0/10 tailnet range) — not this Mac."; \
	echo "    Diagnose + fix: docs/runbooks/tailnet-coordinator-reachability.md"

kill-stray-coordinator: ## Kill any NATIVE (non-Docker) uvicorn coordinator left running (e.g. the stray :8401)
	@pids="$$(pgrep -f 'uvicorn coordinator.app:create_app' 2>/dev/null)"; \
	if [ -z "$$pids" ]; then echo "==> no native coordinator processes running"; else \
		echo "==> killing native coordinator uvicorn pids: $$pids"; \
		echo "$$pids" | xargs kill 2>/dev/null || true; \
		echo "==> done. (Docker coordinator on :8400 is unaffected.)"; \
	fi

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

test: lint test-frontend test-cli test-landing test-coordinator test-api test-go test-sidecar ## Run every local suite: privacy lint, frontend, CLI, landing, coordinator, api (Docker), Go, sidecar

test-fast: LANDING_BUILD=0
test-fast: lint test-frontend test-cli test-landing ## Fast subset: privacy lint + the three node suites with typechecks (no Docker, no Python, no next build)

test-api: check-docker ## Run the api pytest suite in a throwaway api container (starts Postgres + migrations if needed)
	$(COMPOSE) run --rm -T -u root api sh -c 'pip install -q -r requirements-dev.txt && env PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider'

test-frontend: ## Run the desktop frontend vitest suite, then typecheck app, test and vite-config files
	@if [ ! -d apps/desktop/frontend/node_modules ]; then \
		echo "==> apps/desktop/frontend/node_modules missing: run 'make frontend-setup' first"; \
		exit 1; \
	fi
	@cd apps/desktop/frontend && pnpm exec vitest run && pnpm run typecheck

test-cli: ## Run the CLI vitest suite, then typecheck it
	@if [ ! -d apps/cli/node_modules ]; then \
		echo "==> apps/cli/node_modules missing: run 'cd apps/cli && pnpm install' first"; \
		exit 1; \
	fi
	@cd apps/cli && pnpm exec vitest run && pnpm exec tsc --noEmit

LANDING_BUILD ?= 1
test-landing: ## Run the landing vitest suite + typecheck, then a production next build (LANDING_BUILD=0 skips the build)
	@if [ ! -d web/landing/node_modules ]; then \
		echo "==> web/landing/node_modules missing: run 'cd web/landing && pnpm install' first"; \
		exit 1; \
	fi
	@cd web/landing && pnpm exec vitest run && pnpm exec tsc --noEmit
	@if [ "$(LANDING_BUILD)" != "0" ]; then cd web/landing && pnpm exec next build; fi

test-coordinator: ## Run coordinator pytest suite (privacy invariants etc.) in its venv
	@if [ ! -x services/coordinator/.venv/bin/pytest ]; then \
		echo "==> services/coordinator/.venv/bin/pytest missing — run 'make coordinator-setup' first"; \
		exit 1; \
	fi
	@cd services/coordinator && ./.venv/bin/pytest -q

test-sidecar: ## Run the desktop Python sidecar test suite in its venv
	@if [ ! -x apps/desktop/python/.venv/bin/pytest ]; then \
		echo "==> apps/desktop/python/.venv/bin/pytest missing — run 'make sidecar-setup' first"; \
		exit 1; \
	fi
	@cd apps/desktop/python && ./.venv/bin/pytest -q

test-go: ## Run the Go unit tests (mesh sidecar + DERP relay)
	@cd apps/desktop/go/potluck-mesh && go test ./...
	@cd services/derp && go test ./...

m3-smoke: ## End-to-end Phase G m3 acceptance check (needs coordinator + at least one peer running)
	@python3 scripts/dev/m3-smoke.py $(M3_ARGS)

mesh-smoke: ## End-to-end Phase G mesh week-1 acceptance (needs coordinator + Postgres)
	@python3 scripts/dev/mesh-smoke.py $(MESH_ARGS)

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
	@python3 scripts/dev/lint_privacy.py

smoke-prod: ## Post-deploy smoke test against LIVE prod (api.trypotluck.ai + coordinator.trypotluck.ai). Safe/read-only by default.
	@echo "==> Not wired to CI -- run manually after every coordinator/api deploy (see docs/launch/production-launch-checklist.md)."
	@echo "==> Opt-in deeper checks: SMOKE_ARGS='--with-session TOKEN' or SMOKE_ARGS='--send-email you@example.com'"
	@bash scripts/smoke_prod.sh $(SMOKE_ARGS)

check-models: ## Check the model catalog against the curated current-gen lineup (add --write or --json via ARGS)
	@if [ ! -x apps/desktop/python/.venv/bin/python ]; then \
		echo "==> sidecar venv not found — run 'make sidecar-setup' first"; \
		exit 1; \
	fi
	@apps/desktop/python/.venv/bin/python scripts/check_model_updates.py $(ARGS)

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
	@cd apps/desktop/electron && pnpm run prepack:python && pnpm run prepack:mcp && pnpm run prepack:mesh && pnpm run prepack:mesh-helper
	@echo "==> Stamping build info (git sha + build time + dirty flag)"
	@cd apps/desktop/electron && pnpm run prepack:build-info

installer-mac: installer-bundle ## Build SIGNED macOS installer (.dmg + .zip). Auto-discovers the Developer ID cert in the login keychain and signs; notarizes using creds sourced from ~/.potluck-apple-env if present (else APPLE_* already in env). scripts/notarize.js. With no cert/creds it falls back to an unsigned build.
	@echo "==> Building macOS installer (signs with keychain Developer ID; notarizes if APPLE_* env or ~/.potluck-apple-env is present)"
	@sh -c '[ -f "$$HOME/.potluck-apple-env" ] && { echo "==> Sourcing notarization creds from ~/.potluck-apple-env"; . "$$HOME/.potluck-apple-env"; }; cd apps/desktop/electron && pnpm run build:mac'

installer-mac-dir: installer-bundle ## Fast unsigned macOS build: unpacked .app only (no dmg/zip/notarize/hash). For local dogfooding — see 'make dogfood'.
	@echo "==> Building macOS .app (unpacked --dir, unsigned, no notarization)"
	@cd apps/desktop/electron && CSC_IDENTITY_AUTO_DISCOVERY=false pnpm exec electron-builder --mac --arm64 --dir

installer-win: installer-bundle ## Build unsigned Windows installer (.exe via NSIS). Signing requires EV cert.
	@echo "==> Building Windows installer (unsigned — needs CSC_LINK env for signing)"
	@cd apps/desktop/electron && CSC_IDENTITY_AUTO_DISCOVERY=false pnpm run build:win

installer-linux: installer-bundle ## Build Linux installers (.AppImage + .deb + .rpm)
	@echo "==> Building Linux installers (AppImage + deb + rpm)"
	@cd apps/desktop/electron && pnpm run build:linux

# installer-linux-docker — build the Linux .deb + .AppImage entirely inside a
# linux/amd64 container, so a Linux release never depends on a specific bench
# box being reachable. PyInstaller cannot cross-compile, so the whole bundle
# pipeline (Python sidecar incl. llama-cpp-python's compiled C++ backend, the
# Go mesh sidecar, and electron-builder's dpkg/AppImage packaging) runs
# natively inside the container. Ubuntu 22.04 matches the glibc on the
# reference dogfood machine (Pop!_OS 22.04) — see Dockerfile.linux.
#
# Only `deb` + `AppImage` at `x64` are built here (not the shared
# package.json's full [x64,arm64] x [AppImage,deb,rpm] matrix): we only
# compiled the sidecar for x64 in this container, and rpm needs tooling this
# image doesn't carry. Bypasses the `build:linux` npm script (which is fixed
# to the full matrix) and calls electron-builder directly instead.
#
# Output: dist-linux/out/ (kept separate from apps/desktop/electron/out/,
# which is real Mac build output — never mixed with Linux artifacts).
# dist-linux/_src/ is a disposable clone of HEAD, always recreated fresh so
# the build can never pick up uncommitted working-tree changes.
#
# IMPORTANT: use exactly ONE bind mount (the whole checkout at /build).
# Do NOT add a second `-v host:/build/some/nested/path` mount inside it —
# on Docker Desktop for Mac (VirtioFS backend) a bind mount nested inside
# another bind mount's tree has been observed to wipe the ENTIRE outer
# mount's host-side directory once the container exits (not just the nested
# subpath). Electron-builder's own `out/` therefore stays inside the
# container-only checkout and is copied to dist-linux/out/ from the host
# *after* the container exits, as a plain `cp`, never as a mount.
DOCKER_LINUX_IMAGE ?= potluck-linux-builder

installer-linux-docker: ## Build .deb + .AppImage for linux/amd64 inside Docker (no Linux box required). Output: dist-linux/out/
	@command -v docker >/dev/null 2>&1 || { echo "error: docker not found. install Docker Desktop."; exit 1; }
	@docker info >/dev/null 2>&1 || { echo "error: docker daemon is not running. open Docker Desktop: open -a Docker"; exit 1; }
	@echo "==> Building linux/amd64 toolchain image ($(DOCKER_LINUX_IMAGE))"
	docker build --platform linux/amd64 -f apps/desktop/electron/Dockerfile.linux -t $(DOCKER_LINUX_IMAGE) apps/desktop/electron
	@echo "==> Cloning a clean checkout of committed HEAD ($$(git rev-parse --short HEAD)) — working-tree changes are never included"
	@rm -rf dist-linux/_src dist-linux/out
	@mkdir -p dist-linux
	git clone --local --quiet . dist-linux/_src
	@echo "==> Running the containerized build with pinned CPU and Vulkan inference libraries"
	docker run --rm --platform linux/amd64 \
		-v "$(CURDIR)/dist-linux/_src:/build" \
		-w /build \
		$(DOCKER_LINUX_IMAGE) \
		bash -c 'set -euo pipefail; \
			make sidecar-setup PYTHON_BIN=python3.11; \
			make frontend-setup; \
			make electron-setup; \
			make installer-bundle PYTHON_BIN=python3.11; \
			cd apps/desktop/electron && pnpm exec electron-builder --linux deb AppImage --x64 && node scripts/hash-release.js'
	@echo "==> Copying installers out of the container checkout to dist-linux/out/"
	@mkdir -p dist-linux/out
	@cp -v dist-linux/_src/apps/desktop/electron/out/*.deb dist-linux/_src/apps/desktop/electron/out/*.AppImage dist-linux/_src/apps/desktop/electron/out/SHA256SUMS dist-linux/out/ 2>/dev/null || true
	@echo "==> Linux installers:"
	@ls -lh dist-linux/out/*.deb dist-linux/out/*.AppImage 2>/dev/null || echo "    (none found — check the build log above)"

installer-clean: ## Remove staged installer artifacts + electron-builder output
	@cd apps/desktop/electron && pnpm run clean
	@rm -rf apps/desktop/python/dist apps/desktop/python/build
	@rm -rf apps/desktop/go/potluck-mesh/dist

install-mac-app: installer-mac-dir ## Install the freshly-built unpacked .app to /Applications, replacing any existing copy. Does NOT kill running processes first — run 'make kill' (or use 'make dogfood') if the old app might be running.
	@SRC="apps/desktop/electron/out/mac-arm64/Potluck AI.app"; \
	if [ ! -d "$$SRC" ]; then \
		echo "error: built app not found at $$SRC (expected output of 'electron-builder --mac --arm64 --dir')"; \
		exit 1; \
	fi; \
	echo "==> Installing to /Applications (replacing any existing copy)"; \
	rm -rf "/Applications/Potluck AI.app"; \
	ditto "$$SRC" "/Applications/Potluck AI.app"; \
	echo "==> Installed: /Applications/Potluck AI.app"

# make dogfood — the whole loop, one command: kill every running Potluck desktop
# process, do a fresh unsigned unpacked build from the current working tree, install
# it over whatever was in /Applications, then print the installed build's stamp
# (git sha + build time + dirty flag, read back from the just-installed .app so
# what's printed is ALWAYS what's actually on disk, not what we assume we built).
# This is the fix for "I rebuilt but the app is still stale": running this command
# is the only way 'I rebuilt' is allowed to mean anything other than 'the running
# app is replaced.'
dogfood: ## Kill + fresh build + install to /Applications + print the installed build stamp. Run this after every local change instead of trusting a stale running app.
	@$(MAKE) kill
	@$(MAKE) install-mac-app
	@echo "==> Installed build stamp:"
	@INFO="apps/desktop/electron/build-info.json"; \
	VERSION=$$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "/Applications/Potluck AI.app/Contents/Info.plist" 2>/dev/null || echo unknown); \
	SHA=$$(grep -o '"sha": *"[^"]*"' "$$INFO" 2>/dev/null | sed -E 's/.*"([^"]+)"$$/\1/' || echo unknown); \
	BUILT_AT=$$(grep -o '"builtAt": *"[^"]*"' "$$INFO" 2>/dev/null | sed -E 's/.*"([^"]+)"$$/\1/' || echo unknown); \
	DIRTY=$$(grep -o '"dirty": *[a-z]*' "$$INFO" 2>/dev/null | sed -E 's/.*: *//' || echo unknown); \
	MTIME=$$(stat -f "%Sm" "/Applications/Potluck AI.app" 2>/dev/null || echo unknown); \
	echo "    version:   $$VERSION"; \
	echo "    sha:       $$SHA (dirty=$$DIRTY)"; \
	echo "    built at:  $$BUILT_AT"; \
	echo "    installed: /Applications/Potluck AI.app (mtime $$MTIME)"; \
	echo "==> 'I rebuilt' now means 'the running app is replaced.'"

# ---------- teardown (kill everything before rebuilding, so a rebuild can't run against a stale app) ----------

# make kill — tear down every Potluck DESKTOP process so a rebuild is never
# validated against a stale running instance. Deliberately narrow patterns:
#   - Potluck AI.app bundle processes (Electron main + helpers + the mesh
#     daemon/mesh-helper when they run from inside the installed bundle)
#   - the Python sidecar bound to :8321 (packaged binary or dev `python -m
#     potluck_local.app`)
#   - the contributor client (potluck_local.contributor.client)
#   - a dev-built Go mesh binary run standalone outside the app bundle
#   - the dev backend's Docker containers (api/postgres/derp)
# Deliberately does NOT match: `potluck-mcp --stdio` (the MCP memory/task
# server other tooling depends on) or unrelated processes from other repos
# (e.g. a `vite` dev server for a different project). If the mesh daemon is
# running as root (the SMAppService LaunchDaemon, PPID 1) this can't send it
# a signal without sudo — it prints the exact command instead of failing.
kill: ## Tear down all Potluck DESKTOP processes (app bundle, sidecar :8321, mesh, contributor client) + stop dev backend containers. Leaves potluck-mcp and unrelated processes untouched. Idempotent.
	@echo "==> Killing Potluck AI.app bundle processes (Electron main/helpers, mesh daemon, mesh-helper)"
	@for pid in $$(pgrep -f '/Applications/Potluck AI\.app/' 2>/dev/null); do \
		owner="$$(ps -o user= -p $$pid 2>/dev/null | tr -d ' ')"; \
		cmd="$$(ps -o comm= -p $$pid 2>/dev/null)"; \
		if [ "$$owner" = "root" ]; then \
			echo "    root-owned, cannot kill without sudo: pid $$pid [$$cmd]"; \
			echo "    -> sudo kill -9 $$pid"; \
		else \
			echo "    killing pid $$pid [$$cmd] (owner=$$owner)"; \
			kill -9 $$pid 2>/dev/null || true; \
		fi; \
	done
	@echo "==> Killing Python sidecar on :8321"
	@pids="$$(lsof -ti tcp:8321 2>/dev/null)"; \
	if [ -n "$$pids" ]; then \
		for pid in $$pids; do echo "    killing pid $$pid (listening on :8321)"; kill -9 $$pid 2>/dev/null || true; done; \
	else \
		echo "    nothing listening on :8321"; \
	fi
	@pkill -9 -f 'potluck_local\.app' 2>/dev/null && echo "    killed lingering dev sidecar process(es) (potluck_local.app)" || true
	@echo "==> Killing contributor client (potluck_local.contributor.client)"
	@pkill -9 -f 'potluck_local\.contributor\.client' 2>/dev/null && echo "    killed contributor client process(es)" || true
	@echo "==> Killing dev-built Go mesh binary (outside the app bundle), if any"
	@for pid in $$(pgrep -f 'go/potluck-mesh/dist/potluck-mesh' 2>/dev/null); do \
		echo "    killing pid $$pid"; \
		kill -9 $$pid 2>/dev/null || true; \
	done
	@echo "==> Stopping dev backend containers (ignored if not running)"
	@docker stop potluck-api potluck-postgres potluck-derp >/dev/null 2>&1 || true
	@echo "==> make kill done."

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
