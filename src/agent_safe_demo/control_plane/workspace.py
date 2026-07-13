"""The single-workspace controller.

One :class:`Workspace` owns the currently selected app and its StateFork
backend. ``ensure()`` lazily brings the workspace up: build the base (once),
fork a branch from it, take the *Initial snapshot*, and return the payload the
UI renders. Switching apps tears the old backend down and starts over.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from agent_safe_demo.control_plane.app_registry import (
    AppSpec,
    get_app_spec,
    list_app_specs,
    list_manifest_errors,
)
from agent_safe_demo.control_plane.catalog import (
    CatalogStore,
    products_json_for_app,
    shopgym_dir_default,
)
from agent_safe_demo.control_plane.data_tier import DoltServerDataTier
from agent_safe_demo.control_plane.statefork import BranchError, StateForkBackend

logger = logging.getLogger("control_plane.Workspace")

INITIAL_SNAPSHOT_LABEL = "Initial snapshot"

# Storefront data-tier backend: "dolt_server" runs the pricing/inventory overlay
# in an external Dolt sql-server (architecture A). Anything else (default) keeps
# the original in-checkpoint behaviour with no Dolt dependency.
DATA_BACKEND = os.getenv("DEMO_SHOP_DB_BACKEND", "").lower()


class Workspace:
    def __init__(self, app: AppSpec | None = None) -> None:
        self.app = app or get_app_spec()
        self.backend = StateForkBackend.from_env(self.app)
        self._branch_id: str | None = None
        # External Dolt data tier (architecture A). ``dolt_server`` is set by the
        # FastAPI lifespan once the process is up; ``catalog``/``data_tier`` are
        # (re)built per selected app by provision_data_tier().
        self.dolt_server: Any | None = None
        self.catalog: CatalogStore | None = None
        self.data_tier: DoltServerDataTier | None = None

    # ------------------------------------------------------------- data tier

    def attach_dolt_server(self, server: Any) -> None:
        """Called from the app lifespan once the shared dolt sql-server is up."""
        self.dolt_server = server
        self.provision_data_tier()

    def provision_data_tier(self) -> None:
        """(Re)build the external data tier for the currently selected shop:
        ensure its database, seed the pristine catalog, pin a clean baseline, and
        attach the tier + connection env to the active backend. Env-gated and
        best-effort: any failure falls back to serving the baked JSON catalog."""
        self.catalog = None
        self.data_tier = None
        if DATA_BACKEND != "dolt_server" or self.dolt_server is None:
            return
        db = self.app.id  # one Dolt database per shop
        try:
            self.dolt_server.ensure_database(db)
            conn = self.dolt_server.conn_params(db)
            catalog = CatalogStore(conn)
            catalog.ensure_schema()
            tier = DoltServerDataTier(
                host=conn["host"],
                port=conn["port"],
                database=db,
                user=conn["user"],
                password=conn["password"],
            )
            if catalog.is_empty():
                products = products_json_for_app(self.app.id, shopgym_dir_default())
                catalog.seed_from_products_json(
                    products, default_stock=int(os.getenv("DEMO_SHOP_SEED_STOCK", "25"))
                )
                tier.mark_clean()
            else:
                # Reusing an existing db: start this selection from pristine.
                tier.reset_to_clean()
            self.catalog = catalog
            self.data_tier = tier
            self.backend.set_data_tier(
                tier,
                runtime_env={
                    "SHOP_DOLT_HOST": str(conn["host"]),
                    "SHOP_DOLT_PORT": str(conn["port"]),
                    "SHOP_DOLT_DB": db,
                    "SHOP_DOLT_USER": str(conn["user"]),
                    "SHOP_DOLT_PASSWORD": str(conn["password"]),
                },
            )
            logger.info("storefront data tier attached for %s (db=%s)", self.app.id, db)
        except Exception as error:  # dolt down, missing seed, etc.
            logger.warning(
                "data-tier provisioning failed for %s: %s; serving baked JSON only",
                self.app.id,
                error,
            )
            self.catalog = None
            self.data_tier = None

    def _require_catalog(self) -> CatalogStore:
        if self.catalog is None:
            raise BranchError(
                "Catalog data tier is not enabled. Start the control plane with "
                "DEMO_SHOP_DB_BACKEND=dolt_server (and `dolt` on PATH)."
            )
        return self.catalog

    def catalog_list(self, search: str | None = None) -> dict[str, Any]:
        catalog = self._require_catalog()
        return {
            "backend": DATA_BACKEND,
            "database": self.app.id,
            "variants": catalog.list_variants(search=search),
            "changes": catalog.diff_vs_clean(),
        }

    def catalog_update(
        self,
        variant_id: str,
        *,
        price: float | None = None,
        compare_at_price: float | None = None,
        on_hand: int | None = None,
    ) -> dict[str, Any]:
        catalog = self._require_catalog()
        try:
            variant = catalog.update_variant(
                variant_id,
                price=price,
                compare_at_price=compare_at_price,
                on_hand=on_hand,
            )
        except KeyError as error:
            raise BranchError(str(error)) from error
        except ValueError as error:
            raise BranchError(str(error)) from error
        return {"variant": variant, "changes": catalog.diff_vs_clean()}

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
        # Point the data tier at the newly selected shop's database.
        self.provision_data_tier()
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
            # Lets the UI show the catalog editor only when the external Dolt
            # data tier is live for this shop.
            "data_tier": {
                "enabled": self.catalog is not None,
                "backend": DATA_BACKEND or None,
                "database": self.app.id if self.catalog is not None else None,
            },
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
        # Roll the external catalog back to its pristine baseline so Reset clears
        # data edits too (mirrors clearing the in-memory cart).
        if self.data_tier is not None:
            try:
                self.data_tier.reset_to_clean()
            except Exception as error:
                logger.warning("catalog reset_to_clean failed: %s", error)
        return cleanup
