# Agent-Safe Toy Inventory

A small FastAPI web app for experimenting with agent-safe branch workflows.

## Repository Layout

```text
agent_safe_demo/
├── src/agent_safe_demo/       # FastAPI app, branch backends, static UI
├── tests/                     # API tests
├── docs/                      # Ubuntu / checkpoint-lite setup notes
├── scripts/                   # Local run and smoke-test helpers
├── pyproject.toml             # Python project metadata and dependencies
├── requirements.txt           # Convenience install entrypoint
└── README.md                  # Project overview
```

Generated runtime data is ignored by git:

```text
toy_inventory.db
.branches/
build/
dist/
```

## Run Locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
./scripts/run-dev.sh
```

Open `http://127.0.0.1:8000`.

## Test

```bash
pytest
```

For an end-to-end branch smoke test while the dev server is running:

```bash
python scripts/smoke-test.py
```

## Branch Demo

The current branch implementation uses a local-copy backend:

- create branch: copy `toy_inventory.db` into `.branches/<branch_id>/`
- start a separate uvicorn server on `127.0.0.1:8100+`
- run the agent demo against the branch URL
- discard: terminate the branch server
- commit: copy the branch SQLite state back over the main SQLite database

This lets us develop the web workflow on macOS before wiring the real Linux
checkpoint backend.

Useful API endpoints:

- `GET /api/branches`
- `POST /api/branches`
- `POST /api/branches/{branch_id}/run-agent-demo`
- `GET /api/branches/{branch_id}/diff`
- `POST /api/branches/{branch_id}/commit`
- `POST /api/branches/{branch_id}/discard`

## Ubuntu / EC2 Checkpoint-Lite Path

The checkpoint-lite backend uses the same branch API:

```bash
export TOY_BRANCH_BACKEND=checkpoint-lite
export CHECKPOINT_LITE_BIN=/path/to/checkpoint-lite
export TOY_CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions
PYTHONPATH=src uvicorn agent_safe_demo.main:app --host 0.0.0.0 --port 8000
```

The `CheckpointLiteBackend` is in `src/agent_safe_demo/branching.py`. Full VM
setup instructions live in `docs/ubuntu-checkpoint-lite.md`.

Target lifecycle:

```text
create branch -> checkpoint-lite init -> checkpoint-lite create <branch>-base
              -> start branch app URL in the post-checkpoint current layer
run agent     -> HTTP calls against branch URL
discard       -> checkpoint-lite cleanup branch state
commit        -> promote branch state to main
```

## Useful Endpoints

- `GET /api/inventory`
- `POST /api/reservations`
- `POST /api/build-orders`
- `POST /api/build-orders/{id}/try-substitute`
- `POST /api/purchase-orders`
- `GET /api/state`
- `POST /api/reset`

The generated OpenAPI docs are available at `/docs`.
