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

## Run On Shared VM With SSH Port Forwarding

This private repo is currently tested on the shared Ubuntu VM reachable as:

```bash
ssh sf-exp
```

Use SSH port forwarding to view the VM-hosted web UI from your local browser
without opening public inbound ports.

### 1. Open A Tunnel From Your Laptop

Run this on your laptop and keep the terminal open:

```bash
ssh \
  -L 8000:127.0.0.1:8000 \
  -L 8200:127.0.0.1:8200 \
  -L 8201:127.0.0.1:8201 \
  -L 8202:127.0.0.1:8202 \
  sf-exp
```

Port meanings:

- `8000`: main FastAPI app
- `8200+`: checkpoint-lite branch apps

If you create more than three branches at once, add more forwarded ports, for
example `-L 8203:127.0.0.1:8203`.

### 2. Prepare The Repo On The VM

Inside the SSH session:

```bash
cd ~/Web_Demo_For_Checkpointlite
git pull

python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

If the VM was freshly rebuilt and `venv` support is missing:

```bash
sudo apt-get update
sudo apt-get install -y python3.12-venv
```

### 3. Start The Checkpoint-Lite Demo On The VM

Still inside the SSH session:

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate

export TOY_BRANCH_BACKEND=checkpoint-lite
export CHECKPOINT_LITE_BIN=/users/alexxjk/checkpoint-lite/checkpoint-lite
export TOY_CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions
export TOY_BRANCH_HOST=127.0.0.1
export TOY_BRANCH_PORT_START=8200
export TOY_CHECKPOINT_USE_SUDO=1
export PYTHONPATH=src

uvicorn agent_safe_demo.main:app --host 127.0.0.1 --port 8000
```

### 4. Open The UI Locally

On your laptop, open:

```text
http://127.0.0.1:8000
```

Try this flow:

```text
Create Agent Branch -> Run Agent -> Diff -> Open Branch -> Discard
```

The branch URL should look like:

```text
http://127.0.0.1:8200
```

This URL works in your local browser because the SSH tunnel forwards local
`8200` to the VM's `127.0.0.1:8200`.

### 5. Optional Smoke Test On The VM

In another SSH session or after stopping the server:

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate
python scripts/smoke-test.py
```

Expected result:

```text
build_orders delta: +1
purchase_orders delta: +1
audit_log delta: +3
main state after agent: unchanged
```

### 6. Cleanup

If a run is interrupted, clean up ports and checkpoint-lite mounts:

```bash
lsof -tiTCP:8000 -sTCP:LISTEN | xargs -r kill
lsof -tiTCP:8200-8250 -sTCP:LISTEN | xargs -r sudo kill
sudo umount -l /tmp/checkpoint-sessions/*/work 2>/dev/null || true
sudo rm -rf /tmp/checkpoint-sessions /tmp/checkpoint-sessions-info
```

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
