# Agent-Safe Services Runtime

Direct checkpoint-lite prototype for agent-safe web services. This project is intentionally separate from `agent_safe_demo` and does not use StateFork at runtime.

## Shape

```text
normal user -> main counter service -> source state.db
agent       -> branch controller -> branch service -> checkpoint-lite workdir/state.db
```

The controller exposes branch lifecycle APIs and returns proxied branch URLs, so the VM can expose only the controller port through SSH forwarding or Cloudflare Quick Tunnel.

## Run On sf-exp

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate
pip install -e ./agent_safe_services
./agent_safe_services/scripts/cleanup.sh
./agent_safe_services/scripts/run-main.sh
```

In a second terminal:

```bash
cd ~/Web_Demo_For_Checkpointlite
. .venv/bin/activate
./agent_safe_services/scripts/run-controller.sh
```

Default URLs:

```text
main service:      http://127.0.0.1:8100
controller:        http://127.0.0.1:8000
branch services:   http://127.0.0.1:8400+
proxied branch:    http://127.0.0.1:8000/branches/{branch_id}/proxy/...
```

Laptop tunnel, copied from the older README pattern:

```bash
ssh -o ExitOnForwardFailure=yes -L 18000:127.0.0.1:8000 -L 18100:127.0.0.1:8100 sf-exp
```

Cloudflare Quick Tunnel pattern:

```bash
tmux new -d -s agent-safe-controller './agent_safe_services/scripts/run-controller.sh'
tmux new -d -s agent-safe-tunnel './agent_safe_services/scripts/run-cloudflare-quick-tunnel.sh'
tmux capture-pane -pt agent-safe-tunnel -S -80
```

## API Walkthrough

```bash
curl -s -X POST http://127.0.0.1:8100/reset
curl -s -X POST http://127.0.0.1:8100/increment -H 'Content-Type: application/json' -d '{"amount":10,"actor":"user"}'

BRANCH=$(curl -s -X POST http://127.0.0.1:8000/branches -H 'Content-Type: application/json' -d '{"label":"agent plan"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["branch_id"])')

curl -s -X POST http://127.0.0.1:8000/branches/$BRANCH/proxy/decrement -H 'Content-Type: application/json' -d '{"amount":3,"actor":"agent"}'
curl -s http://127.0.0.1:8000/branches/$BRANCH/diff

SNAP=$(curl -s -X POST http://127.0.0.1:8000/branches/$BRANCH/snapshots -H 'Content-Type: application/json' -d '{"label":"before second action"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["snapshot_id"])')
curl -s -X POST http://127.0.0.1:8000/branches/$BRANCH/proxy/increment -H 'Content-Type: application/json' -d '{"amount":100,"actor":"agent"}'
curl -s -X POST http://127.0.0.1:8000/branches/$BRANCH/restore/$SNAP

curl -s -X POST http://127.0.0.1:8000/branches/$BRANCH/commit
# or
curl -s -X DELETE http://127.0.0.1:8000/branches/$BRANCH
```

## Scope

This MVP uses filesystem checkpoints only (`create ... -1`) and restarts branch web processes after restore. CRIU process continuation can be added later as an optional mode.
