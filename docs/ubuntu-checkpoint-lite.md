# Ubuntu / EC2 Checkpoint-Lite Setup

This guide moves the toy inventory branch demo from the local-copy backend to
the first checkpoint-lite backend on an Ubuntu VM or EC2 instance.

## 1. System Requirements

Use Ubuntu on a VM where you have `sudo`.

Required capabilities:

- Linux kernel with OverlayFS support
- CRIU installed and working
- Go toolchain if building checkpoint-lite from source
- Python 3.10+

Install baseline packages:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git curl criu
sudo criu check
```

`sudo criu check` must pass before checkpoint-lite process snapshots can work.
The first toy backend mostly exercises OverlayFS sessions, but CRIU should be
healthy before we move to full process checkpointing.

## 2. Build Checkpoint-Lite

From the parent research directory:

```bash
cd ~/Search_Agent/checkpoint-lite
go build -o checkpoint-lite cmd/checkpoint-lite/main.go
go build -o bash_init cmd/bash-init/main.go
./checkpoint-lite version
```

If `go` is missing, install the version expected by checkpoint-lite or use the
project's existing build notes.

## 3. Run The Toy App Normally First

```bash
cd ~/Search_Agent/agent_safe_demo
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
PYTHONPATH=src uvicorn agent_safe_demo.main:app --host 0.0.0.0 --port 8000
```

From your browser, open:

```text
http://<vm-ip>:8000
```

On EC2, the security group must allow inbound TCP `8000`. For this prototype,
use a single branch port such as `8200` only if you intentionally expose branch
URLs. Prefer SSH forwarding or a reverse proxy for demos.

## 4. Smoke Test Local-Copy Branching On Ubuntu

Before switching backends, confirm the current branch flow works:

```bash
curl -s http://127.0.0.1:8000/api/branches
curl -s -X POST http://127.0.0.1:8000/api/branches
```

Then use the UI:

```text
Create Agent Branch -> Run Agent -> Diff -> Discard
```

## 5. Start With Checkpoint-Lite Backend

Stop the server, then restart it with:

```bash
cd ~/Search_Agent/agent_safe_demo
. .venv/bin/activate

export TOY_BRANCH_BACKEND=checkpoint-lite
export CHECKPOINT_LITE_BIN=../checkpoint-lite/checkpoint-lite
export TOY_BRANCH_HOST=0.0.0.0
export TOY_BRANCH_PORT_START=8200
export TOY_CHECKPOINT_USE_SUDO=1
export TOY_CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions

PYTHONPATH=src uvicorn agent_safe_demo.main:app --host 0.0.0.0 --port 8000
```

Open the main app again and use:

```text
Create Agent Branch -> Open Branch -> Run Agent -> Diff -> Discard
```

Expected first result:

- Main app remains on port `8000`.
- The active branch app starts on `8200`.
- Branch state writes to the checkpoint-lite overlay workdir.
- Main `toy_inventory.db` is not modified until `Commit`.
- Creating a second checkpoint-lite branch while one is running is rejected.

If checkpoint-lite fails with `mount command failed: exit status 32`, verify
the session directory. Some CloudLab images have a checkpoint-lite config that
points at `/mydata2/checkpoint-sessions`; use `TOY_CHECKPOINT_SESSIONS_DIR` to
force a known-good local path such as `/tmp/checkpoint-sessions`.

## 6. What This Backend Does Today

The first `CheckpointLiteBackend` is intentionally conservative, but it now
creates an explicit named base checkpoint for every branch:

```text
create branch -> sudo checkpoint-lite init <project-root> --quiet
              -> sudo checkpoint-lite create <session> <branch>-base -1
              -> start branch uvicorn in the overlay workdir
              -> set TOY_INVENTORY_DB_PATH=<overlay>/toy_inventory.db

run agent     -> HTTP calls against the branch URL
diff          -> SQLite summary diff between main DB and branch DB
discard       -> terminate branch server, checkpoint-lite cleanup
commit        -> copy branch SQLite DB back to main DB, cleanup
```

This validates the web-service branch lifecycle without forcing us to solve
multi-process CRIU restore in the same step.

## 7. Known Limitations

- The current checkpoint-lite and StateFork backends support one active branch at
  a time. Commit or discard the existing branch before creating another.
- The first backend uses `checkpoint-lite init` and `create`; it does not yet
  restore multiple active branches from one shared base session.
- Commit is SQLite promotion, not a general filesystem merge.
- If main state changes while a branch is active, this prototype does not yet
  implement conflict detection.
- Branch server processes are started by the controller, not restored from CRIU.
- InvenTree is intentionally deferred until this lifecycle is stable.

## 8. Next Backend Iteration

After the first Ubuntu smoke test works, the next implementation should add:

- branch creation from a shared named checkpoint
- branch metadata persisted across controller restarts
- conflict detection before commit
- optional process checkpoint/restore for long-running app state
