## Potluck Local: Python sidecar

This directory contains the FastAPI sidecar that runs inside the Potluck AI desktop app. It binds to `127.0.0.1:8321` and handles local LLM inference via `llama-cpp-python`, model downloads, hardware detection, and user settings. The sidecar is packaged into a single frozen binary by PyInstaller and spawned by the Electron main process; end users never interact with Python directly.

### Dev setup

```
cd apps/desktop/python
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m potluck_local.app --port 8321
```

Set `POTLUCK_DEV=1` to enable Swagger UI at `http://127.0.0.1:8321/docs` and verbose logging.

### Coding-agent context discovery

`GET /v1/potluck/context?model=<id>&scope=local|mine` reports the loaded
`context_window` for the same owned-machine route a chat request would use.
It uses gateway authentication, accepts `potluck_model_sha256` to honor an exact
project pin, sends no prompt and runs no inference. The response includes only
model, scope, context window and the route category (`local` or `household`).
Older serving engines may return a null window. This is a capacity snapshot,
not a reservation: reloading the model or failing over can change it.
