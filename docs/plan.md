# StateFork App Wrapper Plan

## Product Direction

This project should primarily demonstrate StateFork as a lightweight wrapper for
existing software, not as a full agent platform.

The core promise is:

> Take an ordinary app, describe how it starts and where its state lives, then
> give it branch, snapshot, restore, and commit workflows through StateFork.

Agents are useful as a demo consumer of those workflows, but they should remain
optional. The main story is that existing app-plane software can become
branchable with minimal changes.

## Goals

- Let existing apps be onboarded through a small manifest instead of custom
  control-plane code.
- Keep app-plane services ordinary: they should not need to know StateFork
  exists.
- Make StateFork lifecycle operations visible and easy to explain: branch,
  snapshot, restore, commit, discard.
- Provide a same-origin runtime proxy so users can interact with the real app UI
  through the control plane.
- Support simple file-backed state first, especially SQLite databases and local
  app state directories.
- Keep agent integration optional and subordinate to the StateFork demo.

## Non-Goals

- Do not build a general-purpose agent platform as the first priority.
- Do not require apps to expose agent tools before they can be managed.
- Do not require the agent to operate the frontend or browser UI.
- Do not make the control plane understand app-specific business logic.
- Do not solve database merge/conflict resolution in the first version.

## Target Architecture

```text
existing app / app_plane service
        |
        | start command + state files from manifest
        v
StateFork runtime manager
        |
        | branch / snapshot / restore / commit
        v
control plane web shell
        |
        | same-origin /runtime proxy
        v
browser shows real app UI + StateFork controls
```

Suggested module boundaries:

```text
src/agent_safe_demo/
  control_plane/
    app_registry.py        # reads app manifests and exposes AppSpec objects
    manifest.py            # parses and validates statefork.yaml files
    runtime_manager.py     # starts runtime processes from manifest commands
    branching.py           # StateFork lifecycle backend
    commit_store.py        # control-plane metadata and app head history
    main.py                # FastAPI routes and shell orchestration
    static/                # workspace UI

  app_plane/
    email_service/
      app.py
      statefork.yaml
    inventory_service/
      app.py
      statefork.yaml
```

## App Manifest

Each app should eventually be registered by a `statefork.yaml` file. The manifest
is the adapter between a normal app and the StateFork control plane.

Example:

```yaml
id: inventory
name: Inventory Service
description: Parts, stock levels, reservations, and reorder actions.

runtime:
  command: ".venv/bin/uvicorn inventory.app:app --host 127.0.0.1 --port ${PORT}"
  cwd: "."
  port_env: "PORT"
  health_path: "/api/state"
  ui_path: "/"

state:
  files:
    - "demo_inventory.db"
  env:
    DEMO_INVENTORY_DB_PATH: "${BRANCH_WORKDIR}/demo_inventory.db"

observability:
  state_summary_path: "/api/state"
```

The first implementation can keep the current Python `AppSpec` registry, then
move to YAML once the runtime contract settles.

## Onboarding Levels

### Level 0: Zero-Code App Wrapper

For apps that can be started with a command and keep state in local files.

Requirements:

- Start command.
- Working directory.
- Local state files or directories.
- Health check path or basic port probe.

No business-code changes are required.

### Level 1: Thin Observability Adapter

For better UI and demo clarity.

Optional app endpoints:

- `GET /health`
- `GET /state-summary`

The control plane can show richer state summaries, but StateFork operations still
work without this layer.

### Level 2: Agent-Ready Adapter

For apps that want structured agent workflows.

Optional app file:

```text
agent_tools.yaml
```

This declares tool names, input schemas, risk levels, and the underlying app API
paths. This is not required for basic StateFork onboarding.

## Control Plane Responsibilities

The control plane should own:

- App manifest loading and validation.
- StateFork base, branch, snapshot, restore, commit, and discard operations.
- Runtime process startup and health checks.
- Same-origin runtime proxy under `/runtime`.
- App head and commit metadata.
- State summaries and file fingerprints for display.
- Optional agent task orchestration later.

The control plane should not own:

- App business logic.
- Direct business database mutations outside a StateFork branch.
- Browser automation for agent workflows.

## StateFork Lifecycle Semantics

Use the following terminology consistently:

- **Branch**: an isolated runtime workspace created from an app head.
- **Snapshot**: a save point inside the current branch.
- **Restore**: move the current branch back to a snapshot.
- **Commit**: promote the current branch state into the app's new canonical head.
- **Discard**: remove the branch without changing the app head.

The UI and APIs should reinforce that snapshot is local to an experiment, while
commit changes what future branches start from.

## Frontend Design

The frontend should center StateFork, not agents.

Primary regions:

- **Runtime**: iframe/proxy view of the real app UI.
- **Workspace Controls**: snapshot, restore, commit, discard/reset.
- **Checkpoint History**: branch-local snapshots.
- **App Head**: current committed app state and commit history.
- **State View**: optional app summary, table counts, file fingerprints.
- **Runtime Status**: backend, method, branch id, checkpoint id, timings.

Agent controls should be secondary, for example a small `Run Demo Agent` button
or an optional task panel.

## Agent Integration Position

Agents should consume the StateFork wrapper, not define it.

Recommended later flow:

```text
agent task -> create/use branch -> call structured app tools -> snapshot ->
produce diff/proposal -> human approves -> commit
```

The agent should call structured APIs or MCP/function tools through the control
plane. It should not drive the browser UI.

A future `agent_plane/` can contain:

```text
agent_plane/
  worker.py
  tasks.py
  tools.py
  policies.py
  prompts/
```

But this should come after the manifest-based StateFork wrapper is clean.

## Implementation Phases

### Phase 1: Stabilize The Wrapper Contract

- Keep the current `AppSpec` registry.
- Document the runtime contract explicitly.
- Ensure every app uses the same control-plane lifecycle:
  `workspace -> snapshot -> restore -> commit -> new workspace from head`.
- Keep `/runtime` as the only browser-facing runtime URL.

### Phase 2: Add Manifest Support

- Add `control_plane/manifest.py`.
- Support `statefork.yaml` in each app folder.
- Generate `AppSpec` from manifest files.
- Keep Python registry as a fallback for complex demo apps.
- Validate manifest fields at startup and show useful errors in `/api/apps`.

### Phase 3: Generalize Runtime Startup

- Replace app-specific uvicorn fields with generic runtime commands.
- Support environment interpolation:
  - `${PORT}`
  - `${BRANCH_WORKDIR}`
  - `${PROJECT_ROOT}`
- Support one or more state files/directories per app.
- Show state file fingerprints in the control UI.

### Phase 4: Improve Commit History

- Keep `commit_store.py` as control-plane metadata.
- Show app head, parent commit, checkpoint id, label, author, and diff summary.
- Make commit the default way to advance an app's canonical runtime state.
- Keep reset/discard separate from commit.

### Phase 5: Optional Agent Tools

- Add optional `agent_tools.yaml` support.
- Expose `GET /api/apps/{app_id}/tools`.
- Add a safe control-plane tool caller endpoint.
- Add a small demo agent that proves branch-safe experimentation.

## Recommended Near-Term Work

1. Write the first `statefork.yaml` for email and inventory.
2. Add a parser that converts those manifests into existing `AppSpec` objects.
3. Move app-specific runtime fields out of `app_registry.py` over time.
4. Keep the current commit flow, but update the UI copy to frame it as app head
   promotion.
5. Add one zero-code or near-zero-code third app to prove the wrapper story.

## Success Criteria

The demo is successful when a new app can be added by providing:

- A folder containing the app.
- A start command.
- State file paths.
- A health check.

After that, without app-specific control-plane code, the user should be able to:

- Open the app runtime through the control plane.
- Make changes in an isolated branch.
- Save snapshots.
- Restore earlier snapshots.
- Commit accepted changes as the new app head.
- Start the next workspace from that committed head.
