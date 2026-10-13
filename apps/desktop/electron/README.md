# Potluck AI: Electron shell

Run `pnpm install && pnpm dev` from this directory to start the full dev stack (Vite frontend on port 5173, Python sidecar on port 8321, and Electron loading from Vite); prerequisites are that the frontend dependencies have already been installed in `../frontend` and the Python virtualenv exists at `../python/.venv` with `potluck_local` installed (see `../python/README.md`). To produce a platform installer run `pnpm build:mac`, `pnpm build:win`, or `pnpm build:linux` after placing the PyInstaller-frozen binary for the target platform in `resources/python/`.
