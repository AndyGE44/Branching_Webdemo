# Email Server Migration Plan

This plan describes how to evolve the current inventory StateFork demo into an
email-server demo. The current inventory implementation is preserved on the
`inventory-demo-baseline` branch. New product/demo work can continue on `main`.

## Why Move To An Email Server Demo

The current inventory demo proves the StateFork lifecycle:

```text
create base -> create branch -> run agent -> snapshots -> diff -> commit/discard
```

But inventory state is mostly a SQLite database, so a reasonable reviewer can
ask why StateFork is necessary instead of copying a database file.

An email server is a better agent-safety story:

- Users understand email immediately.
- Agent mistakes have obvious risk: wrong label, wrong deletion, wrong draft,
  accidental send.
- Email state naturally spans structured data, message bodies, attachments,
  drafts, labels, search indexes, and background processing.
- A forked service environment is easier to justify than a copied database.

The target story:

```text
Open the mailbox workspace.
The app is already running in a StateFork-backed checkpoint runtime.
User or agent triages email inside that runtime.
Click Snapshot to save meaningful checkpoints.
Restore any checkpoint like a game save point.
Keep main mailbox as a stable seed/control plane until review/commit matures.
```

## Preserve The Current Demo

The inventory demo should remain available as a known-good baseline:

```bash
git checkout inventory-demo-baseline
```

Use this branch for:

- current professor demo fallback
- StateFork/checkpoint-lite regression comparison
- proving that future email work did not lose the old workflow

Use `main` for the email-server migration.

## Product Model

The first email demo should be a demo email service, not a full SMTP/IMAP server.
The goal is to demonstrate branch safety, not implement internet mail.

Recommended first model:

```text
Mailbox
├── Inbox
├── Archive
├── Spam
├── Drafts
└── Sent
```

Email fields:

```text
id
from_address
to_address
subject
body
folder
labels
is_read
priority
created_at
updated_at
```

Draft fields:

```text
id
source_email_id
to_address
subject
body
status: draft | approved | sent
created_by: user | agent
created_at
```

Audit log:

```text
id
actor
action
detail
created_at
```

## Initial Agent Plan

Keep the first agent plan deterministic. This keeps the demo reliable while
still making the branch behavior clear.

Example planned agent run:

```text
1. Label billing-related email as "finance"
2. Move obvious spam email to Spam
3. Draft a reply to an urgent customer email
4. Receive one escalation email into Inbox
5. Archive one low-priority notification
```

Agent steps should not automatically create StateFork snapshots. The runtime
should become dirty after agent/user actions, and the user should explicitly
save checkpoint nodes:

```text
Initial checkpoint
└── before agent
    └── after agent
```

The key rule: the agent operates only inside the managed runtime service.

The mailbox runtime should remain a normal web app. Branching APIs belong to a
separate controller/web-shell surface:

```text
agent_safe_demo.app_plane.email_service.app:app
  mailbox business APIs only

agent_safe_demo.control_plane.main:app
  workspace, snapshot, restore, run-agent APIs
```

This mirrors Docker-style management: packaging or checkpointing the program
does not add container-management APIs to the program itself.

## Demo Flow

Recommended UI flow:

```text
1. Open app; workspace runtime is created automatically.
2. Show mailbox from the runtime, not the main seed database.
3. User edits mail or clicks Run Agent.
4. Dirty badge shows unsaved runtime changes.
5. Click Snapshot to save a checkpoint.
6. Restore any checkpoint; if dirty, ask save/discard/cancel.
7. Review diff/commit later when semantic review is ready.
```

Main page sections:

- Mailbox summary
- Message list
- Message detail
- Drafts
- Backend & Snapshot Stats
- Checkpoints
- Diff / Review panel

Top-level actions:

```text
Run Agent
Snapshot
Restore checkpoint
Reset Workspace
```

Avoid exposing manual base/branch actions in the primary UI. Manual user actions
and agent actions both operate in the managed runtime; main remains the stable
seed/control plane until semantic review and commit are ready.

## Diff Design

Email diff should be more semantic than raw JSON.

Recommended diff groups:

```text
Moved messages
Label changes
Drafts created
Read/unread changes
Archived messages
Spam changes
Deleted messages, if deletion is added later
```

Example diff:

```text
Moved:
- "Win a free prize" Inbox -> Spam
- "Weekly CI report" Inbox -> Archive

Labels:
- "Invoice for April" +finance

Drafts:
- Reply drafted for "Urgent: shipment delay"
```

This will be easier to explain than table count deltas.

## Backend Architecture

The backend surface should expose StateFork as the single runtime backend:

```text
StateForkBackend
```

The lifecycle should remain:

```text
create_base()
create_branch()
run_agent_demo()
diff()
commit()
discard()
reset()
```

The email app should replace inventory domain logic, not the branch lifecycle.

Current limitation to keep explicit:

```text
Commit is still application-level promotion.
```

For the demo email service, commit can initially copy/promote the SQLite mailbox
database, as inventory does today. But the code and docs should clearly call
this a prototype commit.

## Stronger Commit Direction

For a more realistic email demo, commit should eventually apply structured
changes instead of overwriting the whole database.

Better commit model:

```text
1. Compute semantic diff between main and branch.
2. Show reviewable changes.
3. Let user approve all or selected changes.
4. Apply approved operations to main.
```

Operation examples:

```text
add_label(message_id, "finance")
move_message(message_id, "Spam")
create_draft(source_email_id, body)
archive_message(message_id)
```

This is more defensible than raw DB backup and closer to real product behavior.

## API Sketch

Main mailbox APIs:

```text
GET  /api/mailbox
GET  /api/messages
GET  /api/messages/{message_id}
POST /api/messages/{message_id}/label
POST /api/messages/{message_id}/move
POST /api/messages/{message_id}/archive
POST /api/drafts
GET  /api/state
POST /api/reset
```

These APIs are served by the ordinary mailbox runtime.

Branch APIs:

```text
GET    /api/backend
GET    /api/bases
POST   /api/bases
DELETE /api/bases/{base_id}
GET    /api/branches
POST   /api/bases/{base_id}/branches
POST   /api/branches/{branch_id}/run-agent-demo
GET    /api/branches/{branch_id}/diff
POST   /api/branches/{branch_id}/commit
POST   /api/branches/{branch_id}/discard
```

These APIs are served by the controller, not by the mailbox runtime.

Workspace APIs:

```text
GET  /api/workspace
GET  /api/workspace/dirty
POST /api/workspace/run-agent
POST /api/workspace/snapshots
POST /api/workspace/restore
POST /api/workspace/reset
```

## Frontend Migration Plan

Replace inventory components with email components:

```text
Inventory grid       -> mailbox/message list
Inventory actions    -> user mail actions
State tables         -> mailbox state dashboard
Inventory diff       -> email semantic diff
Agent snapshot tree  -> checkpoint list
Backend stats        -> keep
Base/branch panels   -> hide behind workspace controller
```

Do not add StateFork imports, branch IDs, or workspace endpoints to the mailbox
business app. The controller can preview mailbox state by talking to the runtime
over HTTP.

Recommended first UI:

- Left column: folder list and message list.
- Right column: selected message body and metadata.
- Lower full-width panels: StateFork backend, checkpoints, diff.

Keep the UI operational rather than marketing-style. This is a research demo
tool, not a landing page.

## Migration Phases

### Phase 1: Email Domain Skeleton

- Replace seed data with sample emails.
- Add messages, labels, drafts, audit tables.
- Implement mailbox read APIs.
- Render mailbox UI.
- Keep branch backend untouched.

Exit criteria:

- App starts locally.
- Mailbox appears.
- Tests cover seed data and mailbox state.

### Phase 2: User Mail Actions

- Add label, move, archive, create draft APIs.
- Add UI buttons for user actions in the managed runtime.
- Update audit log.

Exit criteria:

- User can mutate the runtime mailbox.
- Reset restores sample mailbox.

### Phase 3: Branch Agent Plan

- Replace inventory agent plan with email agent plan.
- Mark branch dirty after agent actions.
- Let users manually save and restore snapshot nodes.
- Move visible UI from base/branch controls to a managed workspace runtime.

Exit criteria:

- Agent run changes runtime state without creating automatic snapshots.
- User can save and restore runtime snapshots.
- Main mailbox is unchanged.
- Runtime mailbox reflects agent actions.

### Phase 4: Semantic Diff

- Replace inventory delta diff with email semantic diff.
- Show message moves, labels, drafts, archives.

Exit criteria:

- Diff is understandable without reading raw JSON.
- Professor can inspect what the agent did in under 30 seconds.

### Phase 5: Commit Review

- First version: commit by advancing the controller's StateFork head snapshot.
- Better version: apply structured approved operations.

Exit criteria:

- Commit behavior is clearly documented.
- UI labels do not imply a production-grade merge while commit is head promotion.

### Phase 6: Deployment Track

- Reuse `docs/ec2-deployment-plan.md`.
- Validate StateFork on EC2.
- Add public demo access only after branch routing and auth are planned.

## Test Plan

Required tests:

- Seed mailbox loads expected messages.
- Workspace auto-starts a runtime with an initial checkpoint.
- User action mutates runtime mailbox.
- Legacy branch creation still works.
- Agent run marks the workspace dirty.
- Diff contains expected label/move/draft changes.
- Discard leaves main unchanged.
- Commit promotes branch changes.
- Reset clears branches and restores seed mailbox.

StateFork smoke:

```text
open workspace
run agent
save snapshot
restore initial checkpoint
verify semantic diff
reset cleanup
```

## Risks

- If the email service still stores everything in one SQLite file, StateFork's
  advantage may still look underused.
- A real email server is significantly larger than a demo mailbox service.
- Commit semantics become harder once drafts, attachments, and message files are
  represented separately.
- Public access requires auth before it is safe to let others operate branches.

## Recommendation

Do not start with a full SMTP/IMAP implementation. Start with a demo mailbox web
service that behaves like email from the user's perspective.

Best next implementation step:

```text
Create an email-server-demo branch from main.
Replace inventory domain tables and UI with mailbox/message/draft primitives.
Keep StateFork backend APIs unchanged.
```
