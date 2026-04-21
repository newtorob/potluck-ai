#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> Potluck AI dev environment setup"
echo "    Repo: ${REPO_ROOT}"
echo

echo "==> [1/5] Python sidecar venv (apps/desktop/python)"
if [ ! -d "apps/desktop/python/.venv" ]; then
  ${PYTHON_BIN} -m venv apps/desktop/python/.venv
fi
# shellcheck disable=SC1091
source apps/desktop/python/.venv/bin/activate
pip install --upgrade pip
pip install -r apps/desktop/python/requirements.txt
deactivate

echo "==> [2/5] Cloud API venv (services/api)"
if [ ! -d "services/api/.venv" ]; then
  ${PYTHON_BIN} -m venv services/api/.venv
fi
# shellcheck disable=SC1091
source services/api/.venv/bin/activate
pip install --upgrade pip
pip install -r services/api/requirements.txt
deactivate

echo "==> [3/5] Frontend deps (apps/desktop/frontend)"
(cd apps/desktop/frontend && pnpm install)

echo "==> [4/5] Electron deps (apps/desktop/electron)"
(cd apps/desktop/electron && pnpm install)

echo "==> [5/5] Starting Postgres via docker compose"
docker compose up -d postgres

echo
echo "==> Waiting for Postgres to be healthy..."
until docker compose exec -T postgres pg_isready -U potluck -d potluck >/dev/null 2>&1; do
  sleep 1
done

echo "==> Running Alembic migrations"
(cd services/api && source .venv/bin/activate && alembic upgrade head)

echo
echo "==> Done. Run these in separate terminals:"
echo "    Cloud API:     cd services/api && source .venv/bin/activate && uvicorn potluck_api.app:create_app --factory --reload --port 8080"
echo "    Desktop app:   cd apps/desktop/electron && pnpm dev"
echo "    (Electron dev starts the Vite dev server + Python sidecar)"
