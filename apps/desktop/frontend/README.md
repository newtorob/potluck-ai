# Potluck frontend

React/TypeScript frontend rendered inside the Electron window. Run `pnpm install && pnpm dev` to start Vite at `http://127.0.0.1:5173`. In development the app expects the Python sidecar to be running separately at `http://127.0.0.1:8321` (start it with `uvicorn potluck_local.app:create_app --factory --port 8321` from `apps/desktop/python/`). Account and membership data is fetched from `https://api.trypotluck.ai`, copy `.env.example` to `.env.local` if you need to override either base URL.
