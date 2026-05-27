from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from urllib import request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent_safe_demo.mailbox_app import state as local_mailbox_state


BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
TIMEOUT = float(os.getenv("SMOKE_TIMEOUT", "60"))


def get(path: str) -> dict:
    with request.urlopen(f"{BASE_URL}{path}", timeout=TIMEOUT) as response:
        return json.loads(response.read())


def get_url(url: str) -> dict:
    with request.urlopen(url, timeout=TIMEOUT) as response:
        return json.loads(response.read())


def post(path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=TIMEOUT) as response:
        return json.loads(response.read())


def main() -> None:
    post("/api/workspace/reset")
    workspace = get("/api/workspace")
    branch = workspace["branch"]
    before_snapshot = post("/api/workspace/snapshots", {"label": "before agent"})[
        "snapshot"
    ]
    agent = post("/api/workspace/run-agent")
    dirty_after_agent = get("/api/workspace/dirty")
    after_snapshot = post("/api/workspace/snapshots", {"label": "after agent"})[
        "snapshot"
    ]
    branch_state = get_url(f"{branch['url']}/api/state")
    main_state = local_mailbox_state()

    branch_messages = {
        message["id"]: {
            "folder": message["folder"],
            "labels": message["labels"],
            "is_read": message["is_read"],
        }
        for message in branch_state["messages"]
    }
    main_messages = {
        message["id"]: {
            "folder": message["folder"],
            "labels": message["labels"],
            "is_read": message["is_read"],
        }
        for message in main_state["messages"]
    }

    result = {
        "workspace_mode": workspace["workspace"]["mode"],
        "branch_id": branch["id"],
        "branch_url": branch["url"],
        "manual_snapshots": [before_snapshot["label"], after_snapshot["label"]],
        "agent_returned_snapshots": [snapshot["label"] for snapshot in agent["snapshots"]],
        "dirty_after_agent": dirty_after_agent["dirty"],
        "agent_action_statuses": [action["status"] for action in agent["actions"]],
        "branch_mailbox_after_agent": {
            "msg-1001": branch_messages["msg-1001"],
            "msg-1002_drafts": [
                draft for draft in branch_state["drafts"]
                if draft["source_message_id"] == "msg-1002"
            ],
            "msg-1003": branch_messages["msg-1003"],
            "msg-1004": branch_messages["msg-1004"],
            "msg-agent-2001": branch_messages["msg-agent-2001"],
        },
        "main_counts_after_agent": {
            "drafts": len(main_state["drafts"]),
            "audit_log": len(main_state["audit_log"]),
        },
        "main_mailbox_after_agent": {
            "msg-1001": main_messages["msg-1001"],
            "msg-1003": main_messages["msg-1003"],
            "msg-1004": main_messages["msg-1004"],
            "has_msg_agent_2001": "msg-agent-2001" in main_messages,
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
