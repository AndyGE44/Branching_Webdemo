"""The single-workspace controller.

One :class:`Workspace` owns the currently selected app and its StateFork
backend. ``ensure()`` lazily brings the workspace up: build the base (once),
fork a branch from it, take the *Initial snapshot*, and return the payload the
UI renders. Switching apps tears the old backend down and starts over.
"""

from __future__ import annotations

from typing import Any

from agent_safe_demo.control_plane.app_registry import (
    AppSpec,
    get_app_spec,
    list_app_specs,
    list_manifest_errors,
)
from agent_safe_demo.control_plane.statefork import StateForkBackend

INITIAL_SNAPSHOT_LABEL = "Initial snapshot"


class Workspace:
    def __init__(self, app: AppSpec | None = None) -> None:
        self.app = app or get_app_spec()
        self.backend = StateForkBackend.from_env(self.app)
        self._branch_id: str | None = None

    # ------------------------------------------------------------- app selection

    def apps_payload(self) -> dict[str, Any]:
        return {
            "current_app_id": self.app.id,
            "apps": [spec.public_dict() for spec in list_app_specs()],
            "manifest_errors": list_manifest_errors(),
        }

    def select_app(self, app_id: str) -> dict[str, Any]:
        """Switch to another app; the current workspace is discarded."""
        next_app = get_app_spec(app_id)  # raises ValueError for unknown ids
        if next_app.id == self.app.id:
            return {"cleanup": {"branches_deleted": 0, "bases_deleted": 0}, **self.apps_payload()}
        cleanup = self.backend.reset()
        self._branch_id = None
        self.app = next_app
        self.backend = StateForkBackend.from_env(next_app)
        return {"cleanup": cleanup, **self.apps_payload()}

    # ------------------------------------------------------------ workspace state

    def running_branch(self) -> dict[str, Any] | None:
        branches = self.backend.list_branches()
        if self._branch_id:
            for branch in branches:
                if branch["id"] == self._branch_id and branch["status"] == "running":
                    return branch
            self._branch_id = None
        for branch in branches:
            if branch["status"] == "running":
                self._branch_id = branch["id"]
                return branch
        return None

    def ensure(self) -> dict[str, Any]:
        """Return the workspace payload, creating base/branch/Initial snapshot
        on first use (or after the previous branch exited)."""
        branch = self.running_branch()
        if branch is None:
            base_id = self.backend.active_base_id
            if base_id is None:
                base = self.backend.create_base(label=f"{self.app.label} workspace start")
                base_id = base["id"]
            branch = self.backend.create_branch(base_id)
            self._branch_id = branch["id"]
            branch = self.backend.save_snapshot(
                branch["id"], label=INITIAL_SNAPSHOT_LABEL
            )["branch"]
        return self.payload(branch)

    def payload(self, branch: dict[str, Any]) -> dict[str, Any]:
        return {
            "app": self.app.public_dict(),
            "workspace": {
                "app_id": self.app.id,
                "base_id": branch.get("base_id"),
                "branch_id": branch["id"],
                "runtime_url": branch["url"],
                "runtime_proxy_url": "/runtime",
                "runtime_ui_url": f"/runtime{self.app.ui_path}",
                "current_snapshot_id": branch.get("current_snapshot_id"),
            },
            "branch": branch,
        }

    # ----------------------------------------------------------------- actions

    def snapshot(self, label: str | None = None) -> dict[str, Any]:
        branch_id = self.ensure()["branch"]["id"]
        result = self.backend.save_snapshot(branch_id, label=label)
        return {**result, "workspace": self.payload(result["branch"])["workspace"]}

    def restore(self, snapshot_id: str) -> dict[str, Any]:
        branch_id = self.ensure()["branch"]["id"]
        result = self.backend.restore_snapshot(branch_id, snapshot_id=snapshot_id)
        return {**result, "workspace": self.payload(result["branch"])["workspace"]}

    def reset(self) -> dict[str, Any]:
        """Tear down the base and branch; the next ensure() rebuilds from scratch."""
        cleanup = self.backend.reset()
        self._branch_id = None
        return cleanup
