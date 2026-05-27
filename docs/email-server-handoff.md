# Email Server Development Handoff

This file is for continuing the email-server demo in a fresh conversation
without losing context.

## Current Branch And Sync State

Work branch:

```bash
split-controller-mailbox-app
```

Phase 1 runtime rename commit:

```bash
82c343d Rename mailbox runtime settings
```

This branch has been pushed to GitHub and pulled on the shared VM `sf-exp`.

The old inventory demo is preserved separately:

```bash
inventory-demo-baseline
```

Do not use the old inventory branch for new email-server work. It is only a
fallback/reference branch.

## Current Project Identity

The demo is now mailbox-first.

Public/runtime names should use mailbox naming:

```bash
DEMO_MAILBOX_DB_PATH
demo_mailbox.db
agent-safe-demo-mailbox
```

Avoid adding new public docs, env vars, filenames, or package names with
`inventory` unless explicitly talking about the preserved legacy baseline.

## What Phase 1 Already Completed

Phase 1 changed the visible app from inventory to mailbox:

- FastAPI title/description now says mailbox.
- SQLite now creates and seeds email-oriented tables:
  - `messages`
  - `message_labels`
  - `drafts`
  - `audit_log`
- Main mailbox read APIs exist:
  - `GET /api/mailbox`
  - `GET /api/messages`
  - `GET /api/messages/{message_id}`
  - `GET /api/state`
- The web UI now shows:
  - mailbox summary
  - message list
  - message detail
  - message/draft state tables
  - backend stats
  - the active workspace runtime
  - manual checkpoints
- The old `Diff` button is hidden from the UI because semantic email diff is
  not implemented yet.
- Tests were rewritten around mailbox seed data, password auth, base/branch
  lifecycle, and reset.
- Local and VM checks passed.

## What Phase 2 Has Started

Mailbox user mutations now exist on the main app state:

- `POST /api/messages/{message_id}/label`
- `POST /api/messages/{message_id}/move`
- `POST /api/messages/{message_id}/archive`
- `POST /api/messages/{message_id}/read`
- `POST /api/messages`
- `POST /api/drafts`

The web UI now exposes simple user controls in `Message Detail`:

- add a label
- move to Inbox/Archive/Spam
- mark read/unread
- archive
- create a draft reply
- view draft message bodies in `Draft Contents`

The visible demo now starts in one managed workspace runtime. Base/branch
creation remains in the backend compatibility API, but the user-facing UI hides
that machinery and presents a checkpoint-style workflow:

```text
open app -> initial runtime checkpoint -> user/agent actions -> Snapshot -> Restore
```

The top-right action area exposes `Run Agent`, `Snapshot`, and workspace reset.
`Run Agent` is just an automated sequence of mailbox operations inside the
runtime.

The app has been split into two API surfaces:

- `agent_safe_demo.mailbox_app:app` is the ordinary mailbox business app. It
  does not import StateFork and does not expose workspace/branch APIs.
- `agent_safe_demo.main:app` is the StateFork workspace controller. It serves
  the checkpoint UI and workspace APIs, and launches runtime branches as the
  plain mailbox app.

This matches the intended StateFork model: the managed web program does not
know it has been branched.

The branch-agent demo now runs a deterministic mailbox plan:

```text
1. Label "Invoice for April services" as finance.
2. Move "Win a free prize today" to Spam.
3. Draft a reply for "Urgent: shipment delay".
4. Receive "Follow-up: customer escalation" into Inbox.
5. Archive "Weekly CI report".
```

Agent/user steps now mark the workspace runtime as unsaved/dirty. They no
longer create automatic snapshot nodes. Users explicitly save checkpoint nodes:

```text
Initial checkpoint
-> before agent
-> after agent
```

Restoring a snapshot checks dirty state first. If the runtime has unsaved
changes, the UI asks whether to save a snapshot, discard the changes, or cancel.

## Important Compatibility Detail

Some legacy inventory code is still intentionally present in
`src/agent_safe_demo/main.py` and `src/agent_safe_demo/branching.py`.

Why it remains:

- The raw diff implementation still reports legacy inventory-oriented deltas.
- Some preserved inventory endpoints remain for backward compatibility while
  the migration continues.

Rule for new work:

- Do not expose legacy inventory endpoints or inventory diffs in the UI.
- Keep expanding the mailbox path instead of the inventory path.
- Once semantic diff is implemented, remove or archive
  the legacy inventory endpoints.

## Key Files

Backend app:

```text
src/agent_safe_demo/main.py
```

Plain mailbox business app:

```text
src/agent_safe_demo/mailbox_app.py
```

Branch backends and current email agent plan:

```text
src/agent_safe_demo/branching.py
```

Frontend:

```text
src/agent_safe_demo/static/index.html
src/agent_safe_demo/static/app.js
src/agent_safe_demo/static/styles.css
```

Tests:

```text
tests/test_api.py
```

Overall migration plan:

```text
docs/email-server-migration-plan.md
```

VM/checkpoint-lite notes:

```text
docs/ubuntu-checkpoint-lite.md
```

## Recommended Next Development Step

Continue the demo improvement by adding non-DB runtime state to the email agent
so StateFork/checkpoint-lite is visibly preserving and forking live service
state, not just SQLite rows.

Completed backend endpoints:

```text
GET /api/workspace
GET /api/workspace/dirty
POST /api/workspace/run-agent
POST /api/workspace/snapshots
POST /api/workspace/restore
POST /api/workspace/reset
POST /api/messages/{message_id}/label
POST /api/messages/{message_id}/move
POST /api/messages/{message_id}/archive
POST /api/messages/{message_id}/read
POST /api/messages
POST /api/drafts
GET /api/branches/{branch_id}/dirty
POST /api/branches/{branch_id}/snapshots
POST /api/branches/{branch_id}/restore
```

Recommended request models:

```text
LabelMessageRequest:
  label: str
  actor: str = "user"

MoveMessageRequest:
  folder: str
  actor: str = "user"

DraftRequest:
  source_message_id: str | None
  to_address: str
  subject: str
  body: str
  created_by: str = "user"
```

The current deterministic email agent plan is in
`src/agent_safe_demo/branching.py`:

```text
branch_action_request()
branch_action_label()
AGENT_DEMO_ACTIONS
run_agent_demo()
```

The visible workspace flow does not promote runtime changes back into main.
Main mailbox remains a stable seed/control plane while the StateFork/checkpoint
runtime is saved and restored. Legacy commit endpoints still exist for API
compatibility and still reject stale base promotion.

## Later Phase: Semantic Diff

After the email agent path works, replace the current legacy inventory diff with
an email semantic diff.

Recommended diff groups:

```text
Moved messages
Label changes
Drafts created
Read/unread changes
Archived messages
Spam changes
```

This should be much easier to explain than raw SQLite table deltas.

## Local Development Commands

From the local repo:

```bash
cd /Users/andyge/Desktop/Research/Search_Agent/agent_safe_demo
git checkout email-server-demo
git pull --ff-only

python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

Run locally without checkpoint-lite:

```bash
PYTHONPATH=src .venv/bin/uvicorn agent_safe_demo.main:app \
  --host 127.0.0.1 \
  --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

This local mode uses `local-copy`; it is for frontend/API development only and
does not demonstrate real checkpoint-lite/StateFork behavior.

## Shared VM Commands

Connect:

```bash
ssh sf-exp
```

Update branch:

```bash
cd ~/Web_Demo_For_Checkpointlite
git checkout email-server-demo
git pull --ff-only

. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

Run the preferred StateFork demo on the VM:

```bash
export DEMO_BRANCH_BACKEND=statefork
export DEMO_STATEFORK_ROOT=/users/alexxjk/StateFork
export DEMO_STATEFORK_CWD=/users/alexxjk/StateFork
export DEMO_STATEFORK_METHOD=ckpt_build
export CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions
export DEMO_BRANCH_HOST=127.0.0.1
export DEMO_BRANCH_PORT_START=8300
export PYTHONPATH=src

sudo -E .venv/bin/uvicorn agent_safe_demo.main:app \
  --host 127.0.0.1 \
  --port 8000
```

Use SSH forwarding from the laptop:

```bash
ssh \
  -o ExitOnForwardFailure=yes \
  -L 18000:127.0.0.1:8000 \
  -L 18300:127.0.0.1:8300 \
  sf-exp
```

The StateFork/checkpoint-lite backends are still single-active-branch in this
prototype. The workspace controller owns that branch from app startup, so the
UI no longer asks users to create branches manually.

Open locally:

```text
http://127.0.0.1:18000
```

## Validation Checklist

Run before committing:

```bash
pytest -q
python -m py_compile src/agent_safe_demo/main.py src/agent_safe_demo/mailbox_app.py src/agent_safe_demo/branching.py scripts/smoke-test.py
node --check src/agent_safe_demo/static/app.js
git diff --check
```

For a quick API smoke test:

```bash
PYTHONPATH=src .venv/bin/uvicorn agent_safe_demo.main:app \
  --host 127.0.0.1 \
  --port 8765

curl -fsS http://127.0.0.1:8765/api/workspace
```

Expected seed state:

```text
5 messages
3 unread messages
1 draft
Inbox count 4
Archive count 1
```

## Known Caveats

- The package import path is still `agent_safe_demo`; only the package
  distribution name changed to `agent-safe-demo-mailbox`.
- The branch backend abstraction is good and should be reused.
- Legacy base/branch endpoints and `run-agent-demo` still exist on the
  controller for compatibility, but the UI and smoke test now use
  `/api/workspace`.
- Runtime branches launch `agent_safe_demo.mailbox_app:app`; do not add
  workspace or branch APIs to the mailbox app.
- The repo now includes a `Dockerfile` so StateFork/checkpoint-lite build mode
  can create a shell-capable packaged runtime.
- The stable VM smoke path still uses StateFork's init mode by default. Set
  `DEMO_STATEFORK_BUILD=1` when explicitly testing checkpoint-lite build mode.
- VM runs may create root-owned database files if started with `sudo`. If the
  app cannot write the default database, clean up generated runtime data:

```bash
sudo rm -f demo_mailbox.db
sudo rm -rf .branches
```

## Suggested Prompt For A New Conversation

Paste this into the new conversation:

```text
We are continuing the Agent-Safe Demo Mailbox demo in
/Users/andyge/Desktop/Research/Search_Agent/agent_safe_demo.

Please read docs/email-server-handoff.md first. We are on branch
split-controller-mailbox-app. The current task is separating the ordinary
mailbox business app from the StateFork workspace controller. Keep mailbox
business APIs in `agent_safe_demo.mailbox_app:app`; keep snapshot/restore/run
agent APIs in `agent_safe_demo.main:app`; runtime branches should launch the
plain mailbox app. Run tests locally, push the branch, and verify on sf-exp.
```
