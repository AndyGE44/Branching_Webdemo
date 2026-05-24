# Email Server Development Handoff

This file is for continuing the email-server demo in a fresh conversation
without losing context.

## Current Branch And Sync State

Work branch:

```bash
email-server-demo
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
TOY_MAILBOX_DB_PATH
TOY_MAILBOX_BRANCH_ID
toy_mailbox.db
agent-safe-toy-mailbox
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
  - base checkpoints
  - branches
- The old `Run Agent` and `Diff` buttons are hidden from the Phase 1 UI because
  they still call the legacy inventory agent plan.
- Tests were rewritten around mailbox seed data, password auth, base/branch
  lifecycle, and reset.
- Local and VM checks passed.

## What Phase 2 Has Started

Mailbox user mutations now exist on the main app state:

- `POST /api/messages/{message_id}/label`
- `POST /api/messages/{message_id}/move`
- `POST /api/messages/{message_id}/archive`
- `POST /api/messages/{message_id}/read`
- `POST /api/drafts`

The web UI now exposes simple user controls in `Message Detail`:

- add a label
- move to Inbox/Archive/Spam
- mark read/unread
- archive
- create a draft reply

The StateFork/checkpoint branch lifecycle is intentionally still separate from
these user controls. Branch cards remain focused on base checkpoints, branch
creation, commit, and discard until the deterministic email agent plan replaces
the legacy inventory agent path.

## Important Compatibility Detail

Some legacy inventory code is still intentionally present in
`src/agent_safe_demo/main.py` and `src/agent_safe_demo/branching.py`.

Why it remains:

- The branch backend's old deterministic agent path still uses legacy inventory
  endpoints internally.
- Phase 1 focused on changing the visible product model to mailbox state without
  rewriting the entire branch agent/diff layer at the same time.

Rule for new work:

- Do not expose legacy inventory endpoints or inventory diffs in the UI.
- Prefer replacing the legacy agent path with email actions instead of expanding
  the inventory path.
- Once the email agent path and semantic diff are implemented, remove or archive
  the legacy inventory endpoints.

## Key Files

Backend app:

```text
src/agent_safe_demo/main.py
```

Branch backends and current legacy agent plan:

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

Continue Phase 2 by replacing the legacy branch-agent plan with deterministic
email actions.

Completed backend endpoints:

```text
POST /api/messages/{message_id}/label
POST /api/messages/{message_id}/move
POST /api/messages/{message_id}/archive
POST /api/messages/{message_id}/read
POST /api/drafts
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

The next backend step should replace the legacy inventory agent plan with an
email agent plan.

The current legacy agent plan is in `src/agent_safe_demo/branching.py`:

```text
branch_action_path()
branch_action_label()
AGENT_DEMO_ACTIONS
run_agent_demo()
```

Target deterministic email agent plan:

```text
1. Label "Invoice for April services" as finance.
2. Move "Win a free prize today" to Spam.
3. Draft a reply for "Urgent: shipment delay".
4. Archive "Weekly CI report".
```

Each step should still create a snapshot node, so the branch card can show:

```text
Base checkpoint
-> label finance
-> move spam
-> draft reply
-> archive report
```

Main mailbox must remain unchanged until commit.

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
export TOY_BRANCH_BACKEND=statefork
export TOY_STATEFORK_ROOT=/users/alexxjk/StateFork
export TOY_STATEFORK_CWD=/users/alexxjk/StateFork
export TOY_STATEFORK_METHOD=ckpt_build
export CHECKPOINT_SESSIONS_DIR=/tmp/checkpoint-sessions
export TOY_BRANCH_HOST=127.0.0.1
export TOY_BRANCH_PORT_START=8300
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
  -L 18301:127.0.0.1:8301 \
  -L 18302:127.0.0.1:8302 \
  sf-exp
```

Open locally:

```text
http://127.0.0.1:18000
```

## Validation Checklist

Run before committing:

```bash
pytest -q
python -m py_compile src/agent_safe_demo/main.py src/agent_safe_demo/branching.py scripts/smoke-test.py
node --check src/agent_safe_demo/static/app.js
git diff --check
```

For a quick API smoke test:

```bash
PYTHONPATH=src .venv/bin/uvicorn agent_safe_demo.main:app \
  --host 127.0.0.1 \
  --port 8765

curl -fsS http://127.0.0.1:8765/api/mailbox
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
  distribution name changed to `agent-safe-toy-mailbox`.
- The branch backend abstraction is good and should be reused.
- The legacy `run-agent-demo` endpoint still exists but should be migrated to
  email actions before it is re-exposed in the UI.
- The old `scripts/smoke-test.py` still exercises the legacy branch-agent path.
  Update it once the email agent path is implemented.
- VM runs may create root-owned database files if started with `sudo`. If the
  app cannot write the default database, clean up generated runtime data:

```bash
sudo rm -f toy_mailbox.db
sudo rm -rf .branches
```

## Suggested Prompt For A New Conversation

Paste this into the new conversation:

```text
We are continuing the Agent-Safe Toy Mailbox demo in
/Users/andyge/Desktop/Research/Search_Agent/agent_safe_demo.

Please read docs/email-server-handoff.md first. We are on branch
email-server-demo. Phase 1 is complete: mailbox read UI/API and renamed mailbox
runtime settings are done. Next, start Phase 2 by adding mailbox mutation APIs
and simple user controls, while keeping StateFork branch lifecycle intact.
Run tests locally, push the branch, and verify on sf-exp.
```
