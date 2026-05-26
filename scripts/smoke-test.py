from __future__ import annotations

import json
import os
from urllib import request


BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")


def get(path: str) -> dict:
    with request.urlopen(f"{BASE_URL}{path}", timeout=10) as response:
        return json.loads(response.read())


def get_url(url: str) -> dict:
    with request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())


def post(path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def main() -> None:
    post("/api/reset")
    branch = post("/api/branches")["branch"]
    agent = post(f"/api/branches/{branch['id']}/run-agent-demo")
    branch_state = get_url(f"{branch['url']}/api/state")
    main_state = get("/api/state")
    post(f"/api/branches/{branch['id']}/discard")

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
        "branch_id": branch["id"],
        "branch_url": branch["url"],
        "snapshots": [snapshot["label"] for snapshot in agent["snapshots"]],
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
