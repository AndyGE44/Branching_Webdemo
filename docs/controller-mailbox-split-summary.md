# Controller / Mailbox Split Summary

This branch separates the demo into two API surfaces so the mailbox service is
managed by StateFork from the outside instead of managing its own branches.

## What Changed

### 1. Plain mailbox business app

Added:

```text
src/agent_safe_demo/mailbox_app.py
```

This is the ordinary web app. It only exposes mailbox business APIs:

```text
GET  /api/mailbox
GET  /api/messages
GET  /api/messages/{message_id}
POST /api/messages/{message_id}/label
POST /api/messages/{message_id}/move
POST /api/messages/{message_id}/archive
POST /api/messages/{message_id}/read
POST /api/messages
POST /api/drafts
GET  /api/state
POST /api/reset
```

It does not import StateFork, does not know about branches, and does not expose
workspace/checkpoint APIs.

### 2. StateFork workspace controller

Kept:

```text
src/agent_safe_demo/main.py
```

But it is now the controller/web-shell layer. It owns the UI and control APIs:

```text
GET  /api/workspace
GET  /api/workspace/dirty
POST /api/workspace/run-agent
POST /api/workspace/snapshots
POST /api/workspace/restore
POST /api/workspace/reset
GET  /api/backend
GET  /api/bases
POST /api/bases
POST /api/bases/{base_id}/branches
GET  /api/branches
```

The controller intentionally does not serve `/api/mailbox`. It talks to the
runtime mailbox app over HTTP.

### 3. Runtime branches now launch the plain app

Updated:

```text
src/agent_safe_demo/branching.py
```

Runtime branches now start:

```text
agent_safe_demo.mailbox_app:app
```

instead of:

```text
agent_safe_demo.main:app
```

This means the managed program is a normal mailbox app and does not know it has
been branched.

### 4. Docker runtime definition

Added:

```text
Dockerfile
.dockerignore
```

The Docker image contains:

```text
python:3.12-slim
bash
ca-certificates
procps
the repo code
FastAPI / uvicorn / package dependencies
```

The image default command runs the ordinary mailbox app:

```text
python -m uvicorn agent_safe_demo.mailbox_app:app --host 0.0.0.0 --port 8000
```

This gives checkpoint-lite/StateFork build mode a packaged, shell-capable
runtime for the managed program.

The VM-stable StateFork path still defaults to init mode. Build mode can be
requested with:

```bash
DEMO_STATEFORK_BUILD=1
```

For build mode, the controller reuses the initial snapshot that StateFork's
`CheckpointLiteBuildManager` creates during `checkpoint-lite build`. That avoids
requesting an immediate duplicate memory checkpoint before the runtime branch is
created.

## Why This Matches The Intended Design

The mailbox app is now analogous to a normal program inside a Docker container:
it only exposes business APIs. Snapshot, restore, and runtime lifecycle are
handled by a separate controller API, similar to Docker/Kubernetes/StateFork
management APIs.

In short:

```text
business API != branching/control API
```

## Validation

Local validation:

```text
20 passed
py_compile passed
node --check passed
git diff --check passed
local smoke test passed
```

`sf-exp` validation:

```text
20 passed
StateFork smoke passed
StateFork restore-to-initial passed
Docker build passed
Docker build-mode workspace/smoke/restore passed with DEMO_STATEFORK_BUILD=1
```

API separation check on `sf-exp`:

```text
controller /api/mailbox -> 404
runtime    /api/workspace -> 404
runtime    /api/mailbox -> 200
```

That confirms the controller and mailbox business APIs are separate.
