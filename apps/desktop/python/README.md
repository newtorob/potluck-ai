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
