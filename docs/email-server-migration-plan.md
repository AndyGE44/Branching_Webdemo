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
Main mailbox is stable.
Create a StateFork base.
Create an agent branch.
Agent triages email in the branch.
Review diff: labels, drafts, archives, spam moves.
Commit accepted changes or discard the branch.
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

The first email demo should be a toy email service, not a full SMTP/IMAP server.
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
4. Archive one low-priority notification
```

Each step should create a StateFork snapshot:

```text
Base checkpoint
└── label finance email
    └── move spam email
        └── draft customer reply
            └── archive notification
```

The key rule: the agent operates only inside the branch service.

## Demo Flow

Recommended UI flow:

```text
1. Show main mailbox.
2. Create Base.
3. Create Branch.
4. Run Agent.
5. Review snapshot tree.
6. Review diff.
7. Open branch mailbox if needed.
8. Commit or Discard.
```

Main page sections:

- Mailbox summary
- Message list
- Message detail
- Drafts
- Backend & Snapshot Stats
- Base Checkpoints
- Agent Branches
- Diff / Review panel

Branch card actions:

```text
Open Branch
Run Agent
Diff
Commit
Discard
```

Avoid manual branch actions in the first version. Manual user actions belong on
the main mailbox; branch changes should be presented as agent exploration.

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

Reuse the existing backend abstraction:

```text
LocalCopyBackend
CheckpointLiteBackend
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

For the toy email service, commit can initially copy/promote the SQLite mailbox
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

## Frontend Migration Plan

Replace inventory components with email components:

```text
Inventory grid       -> mailbox/message list
Inventory actions    -> user mail actions
State tables         -> mailbox state dashboard
Inventory diff       -> email semantic diff
Agent snapshot tree  -> keep
Backend stats        -> keep
Base/branch panels   -> keep
```

Recommended first UI:

- Left column: folder list and message list.
- Right column: selected message body and metadata.
- Lower full-width panels: StateFork backend, bases, branches, diff.

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
- Add UI buttons for user actions on main.
- Update audit log.

Exit criteria:

- User can mutate main mailbox.
- Reset restores sample mailbox.

### Phase 3: Branch Agent Plan

- Replace inventory agent plan with email agent plan.
- Create snapshot after each agent step.
- Render snapshot tree with email-specific labels.

Exit criteria:

- Branch agent run creates 3-4 snapshots.
- Main mailbox is unchanged.
- Branch mailbox reflects agent actions.

### Phase 4: Semantic Diff

- Replace inventory delta diff with email semantic diff.
- Show message moves, labels, drafts, archives.

Exit criteria:

- Diff is understandable without reading raw JSON.
- Professor can inspect what the agent did in under 30 seconds.

### Phase 5: Commit Review

- First version: keep full SQLite promotion.
- Better version: apply structured approved operations.

Exit criteria:

- Commit behavior is clearly documented.
- UI labels do not imply a production-grade merge if we still use DB promotion.

### Phase 6: Deployment Track

- Reuse `docs/ec2-deployment-plan.md`.
- Validate StateFork on EC2.
- Add public demo access only after branch routing and auth are planned.

## Test Plan

Required tests:

- Seed mailbox loads expected messages.
- User action mutates main mailbox.
- Branch creation works.
- Agent run creates expected snapshots.
- Diff contains expected label/move/draft changes.
- Discard leaves main unchanged.
- Commit promotes branch changes.
- Reset clears branches and restores seed mailbox.

StateFork smoke:

```text
create base
create branch
run agent
verify 3-4 snapshots
verify semantic diff
reset cleanup
```

## Risks

- If the email service still stores everything in one SQLite file, StateFork's
  advantage may still look underused.
- A real email server is significantly larger than a toy mailbox service.
- Commit semantics become harder once drafts, attachments, and message files are
  represented separately.
- Public access requires auth before it is safe to let others operate branches.

## Recommendation

Do not start with a full SMTP/IMAP implementation. Start with a toy mailbox web
service that behaves like email from the user's perspective.

Best next implementation step:

```text
Create an email-server-demo branch from main.
Replace inventory domain tables and UI with mailbox/message/draft primitives.
Keep StateFork backend APIs unchanged.
```

