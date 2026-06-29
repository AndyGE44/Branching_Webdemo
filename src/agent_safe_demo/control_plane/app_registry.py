from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_safe_demo.control_plane.manifest import StateForkManifest, interpolate_template, load_manifest

APP_PLANE_DIR = Path(__file__).resolve().parents[1] / "app_plane"
# repo root: control_plane -> agent_safe_demo -> src -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class AppSpec:
    id: str
    label: str
    description: str
    module: str
    uvicorn_target: str
    db_env_var: str
    db_filename: str
    db_path: Path
    project_root: Path
    init_db: Callable[[], None]
    health_path: str = "/api/state"
    state_path: str = "/api/state"
    runtime_ui_path: str = "/"
    agent_demo_label: str = "Run"
    agent_demo_actions: Sequence[dict[str, Any]] | None = None
    manifest_path: Path | None = None
    runtime_command: str | None = None
    runtime_cwd: str = "."
    runtime_port_env: str = "PORT"
    runtime_type: str = "process"
    build_dockerfile_dir: Path | None = None
    state_files: tuple[str, ...] = field(default_factory=tuple)
    state_env: dict[str, str] = field(default_factory=dict)
    # False for manifest-only/container apps with no SQLite file (state captured
    # by the container checkpoint, e.g. the shopgym shops).
    db_backed: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "uvicorn_target": self.uvicorn_target,
            "db_env_var": self.db_env_var,
            "db_filename": self.db_filename,
            "health_path": self.health_path,
            "state_path": self.state_path,
            "runtime_ui_path": self.runtime_ui_path,
            "agent_demo_label": self.agent_demo_label,
            "agent_demo_enabled": bool(self.agent_demo_actions),
            "manifest_loaded": self.manifest_path is not None,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "runtime_command": self.runtime_command,
            "runtime_cwd": self.runtime_cwd,
            "runtime_port_env": self.runtime_port_env,
            "runtime_type": self.runtime_type,
            "build_dockerfile_dir": str(self.build_dockerfile_dir) if self.build_dockerfile_dir else None,
            "state_files": list(self.state_files),
            "state_env_keys": sorted(self.state_env),
            "db_backed": self.db_backed,
        }


def _container_spec_from_manifest(path: Path, manifest: StateForkManifest) -> AppSpec:
    """AppSpec for a manifest-only app whose state is captured entirely by the
    StateFork container checkpoint (the shopgym shops). There is no SQLite file:
    ``db_backed=False`` and ``init_db`` is a no-op."""
    app_dir = path.parent
    db_env_var = next(iter(manifest.state.env), "") if manifest.state.env else ""
    # Sentinel path that never exists, so db `.exists()` checks cleanly skip.
    sentinel_db = REPO_ROOT / f".{manifest.id}-container-state"
    dockerfile_dir: Path | None = None
    if manifest.build is not None:
        raw = interpolate_template(
            manifest.build.dockerfile_dir,
            {"APP_DIR": str(app_dir), "PROJECT_ROOT": str(REPO_ROOT)},
        )
        dd = Path(raw)
        dockerfile_dir = (dd if dd.is_absolute() else app_dir / dd).resolve()
    return AppSpec(
        id=manifest.id,
        label=manifest.name,
        description=manifest.description,
        module="",
        uvicorn_target="",
        db_env_var=db_env_var,
        db_filename="",
        db_path=sentinel_db,
        project_root=REPO_ROOT,
        init_db=lambda: None,
        db_backed=False,
        health_path=manifest.runtime.health_path,
        state_path=manifest.observability.state_summary_path,
        runtime_ui_path=manifest.runtime.ui_path,
        agent_demo_label="Run",
        agent_demo_actions=None,
        manifest_path=path,
        runtime_command=manifest.runtime.command,
        runtime_cwd=manifest.runtime.cwd,
        runtime_port_env=manifest.runtime.port_env,
        runtime_type=manifest.runtime.type,
        build_dockerfile_dir=dockerfile_dir,
        state_files=tuple(manifest.state.files),
        state_env=dict(manifest.state.env),
    )


def _manifest_paths(app_plane_dir: Path) -> list[Path]:
    if not app_plane_dir.exists():
        return []
    return sorted(app_plane_dir.glob("*/statefork.yaml"))


def _manifest_specs(app_plane_dir: Path) -> tuple[dict[str, AppSpec], list[dict[str, str]]]:
    specs: dict[str, AppSpec] = {}
    errors: list[dict[str, str]] = []
    for path in _manifest_paths(app_plane_dir):
        try:
            manifest = load_manifest(path)
            if manifest.id in specs:
                raise ValueError(f"Duplicate manifest id: {manifest.id}")
            specs[manifest.id] = _container_spec_from_manifest(path, manifest)
        except Exception as error:
            errors.append({"path": str(path), "error": str(error)})
    return specs, errors


def _visible_app_ids() -> frozenset[str] | None:
    """User-facing allowlist from DEMO_VISIBLE_APP_IDS (comma-separated). When
    unset, all discovered apps are shown (default). The shopgym launcher sets it
    to the three shops so the App selector lists shops only."""
    raw = os.getenv("DEMO_VISIBLE_APP_IDS", "").strip()
    if not raw:
        return None
    ids = frozenset(part.strip() for part in raw.split(",") if part.strip())
    return ids or None


def build_app_specs(app_plane_dir: Path | None = None) -> dict[str, AppSpec]:
    specs, _ = _manifest_specs(app_plane_dir or APP_PLANE_DIR)
    visible = _visible_app_ids()
    if visible is not None:
        filtered = {app_id: spec for app_id, spec in specs.items() if app_id in visible}
        # Ignore the filter if it would hide everything (misconfigured env), so the
        # selector never ends up empty.
        if filtered:
            return filtered
    return specs


def list_manifest_errors(app_plane_dir: Path | None = None) -> list[dict[str, str]]:
    _, errors = _manifest_specs(app_plane_dir or APP_PLANE_DIR)
    return errors


def list_app_specs() -> list[AppSpec]:
    return list(build_app_specs().values())


def get_app_spec(app_id: str | None = None) -> AppSpec:
    specs = build_app_specs()
    explicit = app_id is not None
    selected = app_id or os.getenv("DEMO_APP_ID", "shop_clothing")
    if selected in specs:
        return specs[selected]
    # For the default/env-resolved app, fall back to the first available app rather
    # than crash. An explicit request for a missing app is still an error.
    if not explicit and specs:
        return specs[sorted(specs)[0]]
    available = ", ".join(sorted(specs))
    raise ValueError(f"Unknown app id: {selected}. Available apps: {available}")
