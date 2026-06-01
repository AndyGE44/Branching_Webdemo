# Ubuntu / EC2 StateFork Setup

This guide documents the supported VM path after the demo was simplified to a
single backend: StateFork. checkpoint-lite may still be used underneath
StateFork, especially in Docker build mode, but the FastAPI controller no longer
exposes a direct checkpoint-lite backend.

## 1. System Requirements

Use Ubuntu on a VM where you have `sudo`.

Required capabilities:

- Linux kernel with OverlayFS support
- CRIU installed and working
- Docker when using `DEMO_STATEFORK_BUILD=1`
- Python 3.10+
- A working StateFork checkout

Install baseline packages:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git curl criu docker.io
sudo criu check
```

## 2. Prepare The Demo

```bash
cd ~/Web_Demo_For_Checkpointlite
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## 3. Start The StateFork Demo

Prefer the launcher:

```bash
./scripts/run-statefork-docker.sh
```

Manual equivalent:

```bash
export DEMO_STATEFORK_BUILD=1
export DEMO_STATEFORK_ROOT=/users/alexxjk/StateFork
export DEMO_STATEFORK_CWD=/users/alexxjk/StateFork
export DEMO_STATEFORK_METHOD=ckpt_build
export CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions-mailbox-demo
export DEMO_BRANCH_HOST=127.0.0.1
export DEMO_BRANCH_PORT_START=8300
export PYTHONPATH=src

sudo -E .venv/bin/uvicorn agent_safe_demo.control_plane.main:app \
  --host 127.0.0.1 \
  --port 8000
```

## 4. Expected Behavior

- The main controller runs on `127.0.0.1:8000`.
- The managed runtime starts at `127.0.0.1:8300` by default.
- `GET /api/backend` reports `backend=statefork`.
- A second active branch is rejected until the current runtime is committed,
  discarded, or reset.
- Runtime state writes to the StateFork-managed workdir DB, not directly to the
  main `demo_mailbox.db`.

## 5. Cleanup

If a run is interrupted:

```bash
./scripts/cleanup-statefork-demo.sh
```

The cleanup script stops the main/runtime ports and removes StateFork session
state where possible.
