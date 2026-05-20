from __future__ import annotations

import json
from urllib import request


BASE_URL = "http://127.0.0.1:8000"


def get(path: str) -> dict:
    with request.urlopen(f"{BASE_URL}{path}", timeout=10) as response:
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
    main_state = get("/api/state")
    post(f"/api/branches/{branch['id']}/discard")

    result = {
        "branch_id": branch["id"],
        "branch_url": branch["url"],
        "diff_counts": agent["diff"]["counts"],
        "main_counts_after_agent": {
            key: len(main_state[key])
            for key in ["reservations", "build_orders", "purchase_orders", "audit_log"]
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
