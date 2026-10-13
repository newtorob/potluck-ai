# Potluck cloud API

FastAPI service (`potluck_api`) for member accounts: magic-link sign-in, CLI device login, billing, the model catalog, Discourse SSO, admin endpoints and the client version handshake (`GET /client/version`). It has no endpoint that receives prompts or responses.

It shares one Postgres with `services/coordinator`, which has its own Alembic chain. See [`../coordinator/README.md`](../coordinator/README.md).

## Run locally

From the repo root, the Makefile drives the docker-compose stack (Postgres, the API, the coordinator):

```sh
make up          # build and start; runs migrations first
make api-logs    # tail the API logs
make restart     # rebuild and restart the API container
make migrate     # re-run Alembic migrations in a one-shot container
make psql        # psql shell on the dev database
make api-shell   # bash inside the API container
make down        # stop the stack, keep the data volume
```

The API listens on <http://localhost:8080> (`GET /health` for liveness, `GET /ready` for database readiness). `docker-compose.yml` sets development values for every setting, mounts `services/api` into the container and runs `uvicorn` with `--reload`.

To run the API natively against the compose Postgres:

```sh
cd services/api
python3.11 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env                 # then edit the values
./.venv/bin/alembic upgrade head
./.venv/bin/uvicorn potluck_api.app:create_app --factory --reload --port 8080
```

Settings live in `potluck_api/config.py`; `.env.example` lists them.

## Tests

```sh
make test-api
```

This runs `pytest` in a throwaway API container and starts Postgres and the migrations if they are not running. The tests use a dedicated `potluck_test` database on the same Postgres server, create the schema once per session and truncate every table after each test.

Natively, from `services/api` with the dev requirements installed and Postgres running:

```sh
./.venv/bin/pytest --tb=short
```
